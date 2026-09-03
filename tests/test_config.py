"""Tests for config models and loader."""

import os
import re
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from jailbee.config import (
    Config,
    ConfigError,
    Golden,
    HostDevice,
    HostMount,
    SharedCache,
    Stacks,
    load_config,
)
from tests.conftest import make_cfg, with_agent

FIXTURES = Path(__file__).parent / "fixtures"


def _make_config(tmp_path, content: str = "{}\n") -> Path:
    repo_root = tmp_path / "myrepo"
    (repo_root / ".gie").mkdir(parents=True)
    cfg_path = repo_root / ".gie" / "config.yaml"
    cfg_path.write_text(content)
    return cfg_path


# ---------- New schema: defaults, computed attrs, rejection of removed blocks


def test_load_empty_config_uses_all_defaults(tmp_path, mocker):
    mocker.patch("jailbee.config.detect_default_branch", return_value="main")
    cfg_path = _make_config(tmp_path, "{}\n")

    cfg = load_config(cfg_path)

    assert cfg.container_user.uid == os.getuid()
    assert cfg.container_user.gid == os.getgid()
    assert cfg.host_mounts == []
    assert cfg.optional_mounts == {}


def test_after_new_defaults_to_none(tmp_path, mocker):
    """`after_new` defaults to 'none' so existing setups behave unchanged."""
    mocker.patch("jailbee.config.detect_default_branch", return_value="main")
    cfg_path = _make_config(tmp_path, "{}\n")

    cfg = load_config(cfg_path)

    assert cfg.after_new == "none"


@pytest.mark.parametrize("value", ["shell", "tmux", "none"])
def test_after_new_accepts_valid_modes(tmp_path, mocker, value):
    mocker.patch("jailbee.config.detect_default_branch", return_value="main")
    cfg_path = _make_config(tmp_path, f"after_new: {value}\n")

    cfg = load_config(cfg_path)

    assert cfg.after_new == value


def test_after_new_rejects_unknown_mode(tmp_path, mocker):
    mocker.patch("jailbee.config.detect_default_branch", return_value="main")
    cfg_path = _make_config(tmp_path, "after_new: bash\n")

    with pytest.raises(ConfigError):
        load_config(cfg_path)


def test_ssh_seed_from_host_defaults_to_true(tmp_path, mocker):
    """The SSH seed flag defaults to True so existing setups get the
    new behavior without YAML changes."""
    mocker.patch("jailbee.config.detect_default_branch", return_value="main")
    cfg_path = _make_config(tmp_path, "{}\n")

    cfg = load_config(cfg_path)

    assert cfg.ssh.seed_from_host is True


def test_ssh_seed_from_host_can_be_disabled(tmp_path, mocker):
    """Setting `ssh.seed_from_host: false` in YAML disables seeding."""
    mocker.patch("jailbee.config.detect_default_branch", return_value="main")
    cfg_path = _make_config(tmp_path, "ssh:\n  seed_from_host: false\n")

    cfg = load_config(cfg_path)

    assert cfg.ssh.seed_from_host is False


def test_default_shared_caches_includes_ssh():
    """The default shared_caches list includes an `ssh` entry mapping
    <shared_dir>/ssh → ~/.ssh in the container."""
    from jailbee.config import _default_shared_caches

    caches = _default_shared_caches()
    by_name = {c.name: c for c in caches}
    assert "ssh" in by_name
    assert by_name["ssh"].host_subpath == "ssh"
    assert by_name["ssh"].container_path == "~/.ssh"


def test_default_shared_caches_is_ssh_only():
    from jailbee.config import _default_shared_caches

    assert [c.name for c in _default_shared_caches()] == ["ssh"]


def test_load_config_sets_repo_root_from_path(tmp_path, mocker):
    mocker.patch("jailbee.config.detect_default_branch", return_value="main")
    cfg_path = _make_config(tmp_path, "{}\n")

    cfg = load_config(cfg_path)

    assert cfg.repo_root == tmp_path / "myrepo"


def test_load_config_sets_default_branch_from_git(tmp_path, mocker):
    mocked = mocker.patch("jailbee.config.detect_default_branch", return_value="dev")
    mocker.patch("jailbee.config.detect_upstream_remote", return_value="origin")
    cfg_path = _make_config(tmp_path, "{}\n")

    cfg = load_config(cfg_path)

    assert cfg.default_branch == "dev"
    mocked.assert_called_once_with(tmp_path / "myrepo", "origin")


def test_load_config_sets_upstream_remote_from_git(tmp_path, mocker):
    """The upstream remote is detected, not configured — same class of value as
    `default_branch`, which is why neither is a YAML key."""
    mocker.patch("jailbee.config.detect_default_branch", return_value="main")
    mocker.patch("jailbee.config.detect_upstream_remote", return_value="public")
    cfg_path = _make_config(tmp_path, "{}\n")

    cfg = load_config(cfg_path)

    assert cfg.upstream_remote == "public"


def test_load_config_resolves_default_branch_against_the_detected_remote(tmp_path, mocker):
    """`refs/remotes/<remote>/HEAD`, not `refs/remotes/origin/HEAD`."""
    detect_branch = mocker.patch("jailbee.config.detect_default_branch", return_value="main")
    mocker.patch("jailbee.config.detect_upstream_remote", return_value="public")
    cfg_path = _make_config(tmp_path, "{}\n")

    load_config(cfg_path)

    detect_branch.assert_called_once_with(tmp_path / "myrepo", "public")


def test_load_config_upstream_remote_falls_back_to_origin_when_unresolvable(tmp_path, mocker):
    """An ambiguous repo is left exactly where it was before the fix, not broken further."""
    mocker.patch("jailbee.config.detect_default_branch", return_value="main")
    mocker.patch("jailbee.config.detect_upstream_remote", return_value=None)
    cfg_path = _make_config(tmp_path, "{}\n")

    cfg = load_config(cfg_path)

    assert cfg.upstream_remote == "origin"


def test_load_config_sets_container_prefix_from_repo_name(tmp_path, mocker):
    mocker.patch("jailbee.config.detect_default_branch", return_value="main")
    cfg_path = _make_config(tmp_path, "{}\n")

    cfg = load_config(cfg_path)

    assert cfg.container_prefix == "myrepo"


def test_load_config_shared_dir_default_is_under_local_share_gie(tmp_path, mocker, monkeypatch):
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    mocker.patch("jailbee.config.detect_default_branch", return_value="main")
    cfg_path = _make_config(tmp_path, "{}\n")

    cfg = load_config(cfg_path)

    assert cfg.shared_dir == (Path.home() / ".local" / "share" / "jailbee" / "shared" / "myrepo")


def test_load_config_shared_dir_default_honors_xdg_data_home(
    tmp_path,
    mocker,
    monkeypatch,
):
    """$XDG_DATA_HOME overrides the default shared_dir base."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    mocker.patch("jailbee.config.detect_default_branch", return_value="main")
    cfg_path = _make_config(tmp_path, "{}\n")

    cfg = load_config(cfg_path)

    assert cfg.shared_dir == tmp_path / "xdg-data" / "jailbee" / "shared" / "myrepo"


def test_load_config_shared_dir_explicit_override(tmp_path, mocker):
    mocker.patch("jailbee.config.detect_default_branch", return_value="main")
    cfg_path = _make_config(tmp_path, yaml.safe_dump({"shared_dir": "/tmp/explicit"}))

    cfg = load_config(cfg_path)

    assert cfg.shared_dir == Path("/tmp/explicit")


def test_load_config_shared_dir_default_follows_container_prefix_override(
    tmp_path, mocker, monkeypatch
):
    """`shared_dir` default tracks `container_prefix`, not the repo folder name.

    Two checkouts of the same repo folder name disambiguated via
    `container_prefix:` must also get distinct `shared_dir` paths.
    """
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    mocker.patch("jailbee.config.detect_default_branch", return_value="main")
    cfg_path = _make_config(tmp_path, yaml.safe_dump({"container_prefix": "custom-prefix"}))

    cfg = load_config(cfg_path)

    assert cfg.container_prefix == "custom-prefix"
    assert cfg.shared_dir == (
        Path.home() / ".local" / "share" / "jailbee" / "shared" / "custom-prefix"
    )


def test_load_config_rejects_source_repo_block(tmp_path, mocker):
    mocker.patch("jailbee.config.detect_default_branch", return_value="main")
    cfg_path = _make_config(tmp_path, yaml.safe_dump({"source_repo": {"path": "/tmp"}}))

    with pytest.raises(ConfigError):
        load_config(cfg_path)


def test_load_config_rejects_docker_registry_mirror_block(tmp_path, mocker):
    mocker.patch("jailbee.config.detect_default_branch", return_value="main")
    cfg_path = _make_config(tmp_path, yaml.safe_dump({"docker_registry_mirror": {"port": 5000}}))

    with pytest.raises(ConfigError):
        load_config(cfg_path)


def test_load_config_rejects_container_user_username(tmp_path, mocker):
    """`username` was removed — always hardcoded to 'dev' inside container."""
    mocker.patch("jailbee.config.detect_default_branch", return_value="main")
    cfg_path = _make_config(
        tmp_path,
        yaml.safe_dump({"container_user": {"username": "alice", "uid": 4242, "gid": 4242}}),
    )

    with pytest.raises(ConfigError):
        load_config(cfg_path)


def test_load_config_explicit_user_override(tmp_path, mocker):
    mocker.patch("jailbee.config.detect_default_branch", return_value="main")
    cfg_path = _make_config(
        tmp_path,
        yaml.safe_dump(
            {
                "container_user": {"uid": 4242, "gid": 4242},
            }
        ),
    )

    cfg = load_config(cfg_path)

    assert cfg.container_user.uid == 4242
    assert cfg.container_user.gid == 4242


def test_load_config_explicit_path_outside_dot_gie(tmp_path, mocker):
    """`gie -c /random/path.yaml` accepts the path; repo_root = path.parent.parent."""
    mocker.patch("jailbee.config.detect_default_branch", return_value="main")
    weird = tmp_path / "weird.yaml"
    weird.write_text("{}\n")

    cfg = load_config(weird)

    # path.parent == tmp_path; path.parent.parent == tmp_path.parent
    assert cfg.repo_root == tmp_path.parent


# ---------- Fixture-driven tests retained from old suite


def test_load_full_config(mocker):
    mocker.patch("jailbee.config.detect_default_branch", return_value="main")
    cfg = load_config(FIXTURES / "full_config.yaml")
    assert cfg.container_user.uid == 53023
    assert len(cfg.host_mounts) == 2
    assert cfg.host_mounts[0].host == Path.home() / ".gnupg"
    assert cfg.host_mounts[0].readonly is True
    assert "aws" in cfg.optional_mounts
    assert cfg.egress_allow == ["github.com"]
    assert cfg.jetbrains.ide == "idea"
    assert cfg.jetbrains.autostart is True
    assert cfg.terminal.kitty.enabled == "auto"
    assert cfg.terminal.kitty.host_terminfo_path is None
    assert cfg.docker_registry_mirror.extra_registries == [
        "803520778560.dkr.ecr.eu-north-1.amazonaws.com",
    ]
    # Step list shape
    step_names = [s.name for s in cfg.autostart.on_create]
    assert step_names == ["dev_env", "backend", "frontend"]


def test_host_mount_paths_expanded():
    mount = HostMount(host="~/.gnupg", container="/home/dev/.gnupg", readonly=True)
    assert mount.host == Path.home() / ".gnupg"


def test_invalid_yaml_raises():
    with pytest.raises(ConfigError):
        load_config(Path("/nonexistent/config.yaml"))


# ---------- validate_runtime against new repo_root attribute


def _make_runtime_cfg(
    tmp_path,
    *,
    repo_path: Path | None = None,
    host_mounts: list[dict] | None = None,
    optional_mounts: dict[str, dict] | None = None,
) -> Config:
    # Defaults give a clean slate for validate_runtime: jetbrains.enabled
    # and chrome.enabled both default to False, so neither block's
    # host-path checks fire. Tests that need those checks set the
    # `enabled` flag themselves.
    cfg = Config.model_validate(
        {
            "host_mounts": host_mounts or [],
            "optional_mounts": optional_mounts or {},
        }
    )
    repo = repo_path if repo_path is not None else tmp_path / "repo"
    object.__setattr__(cfg, "repo_root", repo)
    object.__setattr__(cfg, "default_branch", "main")
    object.__setattr__(cfg, "container_prefix", repo.name)
    object.__setattr__(cfg, "shared_dir", tmp_path / "shared")
    return cfg


def test_validate_runtime_passes_for_existing_paths(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    cfg = _make_runtime_cfg(tmp_path, repo_path=repo)
    assert cfg.validate_runtime() == []


def test_validate_runtime_reports_missing_repo(tmp_path):
    cfg = _make_runtime_cfg(tmp_path, repo_path=tmp_path / "missing")
    issues = cfg.validate_runtime()
    assert any("repo_root" in issue for issue in issues)


def test_validate_runtime_flags_deprecated_golden_python(tmp_path):
    """A stale `golden.python` pin is a soft, non-blocking deprecation
    warning — the config still loads and validate_runtime just reports it."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    cfg = Config.model_validate({"golden": {"python": "3.13"}})
    object.__setattr__(cfg, "repo_root", repo)
    object.__setattr__(cfg, "default_branch", "main")
    object.__setattr__(cfg, "container_prefix", repo.name)
    object.__setattr__(cfg, "shared_dir", tmp_path / "shared")
    issues = cfg.validate_runtime()
    assert any("golden.python" in issue for issue in issues)


def test_validate_runtime_flags_unparseable_loose_after(tmp_path):
    """`loose_auto_revert.after` is only checked by `.duration()`.

    Nothing on the load path parses it, so `jailbee config validate` is the
    only place that can tell the user before `jailbee net loose` does.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = Config.model_validate({"loose_auto_revert": {"after": "30min"}})
    object.__setattr__(cfg, "repo_root", repo)
    object.__setattr__(cfg, "default_branch", "main")
    object.__setattr__(cfg, "container_prefix", repo.name)
    object.__setattr__(cfg, "shared_dir", tmp_path / "shared")
    issues = cfg.validate_runtime()
    assert any("loose_auto_revert.after" in issue and "30min" in issue for issue in issues)


def test_validate_runtime_flags_loose_after_over_the_cap(tmp_path):
    """The 24h cap is the same class of problem as an unparseable unit."""
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = Config.model_validate({"loose_auto_revert": {"after": "30h"}})
    object.__setattr__(cfg, "repo_root", repo)
    object.__setattr__(cfg, "default_branch", "main")
    object.__setattr__(cfg, "container_prefix", repo.name)
    object.__setattr__(cfg, "shared_dir", tmp_path / "shared")
    assert any("loose_auto_revert.after" in issue for issue in cfg.validate_runtime())


def test_validate_runtime_silent_for_disabled_loose_policy(tmp_path):
    """A disabled policy is never parsed, so its `after` is not a problem."""
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = Config.model_validate({"loose_auto_revert": {"enabled": False, "after": "30min"}})
    object.__setattr__(cfg, "repo_root", repo)
    object.__setattr__(cfg, "default_branch", "main")
    object.__setattr__(cfg, "container_prefix", repo.name)
    object.__setattr__(cfg, "shared_dir", tmp_path / "shared")
    assert cfg.validate_runtime() == []


def test_validate_runtime_does_not_require_git_repo(tmp_path):
    """Mount mode works on plain directories; .git is no longer required."""
    repo = tmp_path / "not-git"
    repo.mkdir()
    cfg = _make_runtime_cfg(tmp_path, repo_path=repo)
    assert cfg.validate_runtime() == []


def test_validate_runtime_reports_missing_host_mount(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    cfg = _make_runtime_cfg(
        tmp_path,
        repo_path=repo,
        host_mounts=[
            {"host": str(tmp_path / "missing-mount"), "container": "/x", "readonly": True}
        ],
    )
    issues = cfg.validate_runtime()
    assert any("host_mounts[0]" in issue for issue in issues)


# ---------- container_prefix YAML field + validation


def _write_repo(tmp_path, *, name="myrepo", config_yaml="{}"):
    """Create <tmp>/<name>/.git and <tmp>/<name>/.jailbee/config.yaml."""
    repo = tmp_path / name
    (repo / ".git").mkdir(parents=True)
    (repo / ".jailbee").mkdir()
    (repo / ".jailbee" / "config.yaml").write_text(config_yaml)
    return repo


def test_container_prefix_defaults_to_repo_root_name(tmp_path, mocker):
    mocker.patch("jailbee.config.detect_default_branch", return_value="main")
    repo = _write_repo(tmp_path, name="myrepo")
    cfg = load_config(repo / ".jailbee" / "config.yaml")
    assert cfg.container_prefix == "myrepo"


def test_container_prefix_yaml_override(tmp_path, mocker):
    mocker.patch("jailbee.config.detect_default_branch", return_value="main")
    repo = _write_repo(tmp_path, name="myrepo", config_yaml="container_prefix: custom-name\n")
    cfg = load_config(repo / ".jailbee" / "config.yaml")
    assert cfg.container_prefix == "custom-name"


def test_container_prefix_invalid_repo_name_requires_override(tmp_path, mocker):
    mocker.patch("jailbee.config.detect_default_branch", return_value="main")
    repo = _write_repo(tmp_path, name="My_Repo")
    with pytest.raises(ConfigError, match="container_prefix"):
        load_config(repo / ".jailbee" / "config.yaml")


def test_container_prefix_invalid_yaml_override_rejected(tmp_path, mocker):
    mocker.patch("jailbee.config.detect_default_branch", return_value="main")
    repo = _write_repo(tmp_path, name="myrepo", config_yaml="container_prefix: Bad_Name\n")
    with pytest.raises(ConfigError, match="container_prefix"):
        load_config(repo / ".jailbee" / "config.yaml")


def test_golden_alias_default_uses_prefix(tmp_path, mocker):
    mocker.patch("jailbee.config.detect_default_branch", return_value="main")
    repo = _write_repo(tmp_path, name="myrepo")
    cfg = load_config(repo / ".jailbee" / "config.yaml")
    assert cfg.golden.alias == "myrepo-base"


def test_golden_alias_explicit_preserved(tmp_path, mocker):
    mocker.patch("jailbee.config.detect_default_branch", return_value="main")
    repo = _write_repo(tmp_path, name="myrepo", config_yaml="golden:\n  alias: my-custom-image\n")
    cfg = load_config(repo / ".jailbee" / "config.yaml")
    assert cfg.golden.alias == "my-custom-image"


# ---------- provision_script + provision_env


def test_golden_provision_script_default_none(tmp_path, mocker):
    mocker.patch("jailbee.config.detect_default_branch", return_value="main")
    repo = _write_repo(tmp_path, name="myrepo")
    cfg = load_config(repo / ".jailbee" / "config.yaml")
    assert cfg.golden.provision_script is None
    assert cfg.golden.provision_env == {}


def test_golden_provision_script_path_preserved(tmp_path, mocker):
    mocker.patch("jailbee.config.detect_default_branch", return_value="main")
    repo = _write_repo(
        tmp_path,
        name="myrepo",
        config_yaml="golden:\n  provision_script: ./.gie/install.sh\n",
    )
    cfg = load_config(repo / ".jailbee" / "config.yaml")
    assert cfg.golden.provision_script == Path("./.gie/install.sh")


def test_provision_env_extra_keys_pass_through(tmp_path, mocker):
    mocker.patch("jailbee.config.detect_default_branch", return_value="main")
    repo = _write_repo(
        tmp_path,
        name="myrepo",
        config_yaml=("golden:\n  provision_env:\n    EXTRA_PKG: redis\n    REGION: eu-north-1\n"),
    )
    cfg = load_config(repo / ".jailbee" / "config.yaml")
    assert cfg.golden.provision_env == {
        "EXTRA_PKG": "redis",
        "REGION": "eu-north-1",
    }


@pytest.mark.parametrize(
    "reserved",
    [
        "CONTAINER_UID",
        "CONTAINER_GID",
        "JAVA_PACKAGE",
        "NODE_MAJOR",
        "EXTRA_APT_PACKAGES",
    ],
)
def test_provision_env_reserved_key_raises(tmp_path, mocker, reserved):
    mocker.patch("jailbee.config.detect_default_branch", return_value="main")
    repo = _write_repo(
        tmp_path,
        name="myrepo",
        config_yaml=f"golden:\n  provision_env:\n    {reserved}: hacker\n",
    )
    with pytest.raises(ConfigError, match=reserved):
        load_config(repo / ".jailbee" / "config.yaml")


# ---------- golden.extra_apt_packages


def test_golden_extra_apt_packages_default_empty(tmp_path, mocker):
    mocker.patch("jailbee.config.detect_default_branch", return_value="main")
    repo = _write_repo(tmp_path, name="myrepo")
    cfg = load_config(repo / ".jailbee" / "config.yaml")
    assert cfg.golden.extra_apt_packages == []


def test_golden_extra_apt_packages_pass_through(tmp_path, mocker):
    mocker.patch("jailbee.config.detect_default_branch", return_value="main")
    repo = _write_repo(
        tmp_path,
        name="myrepo",
        config_yaml=(
            "golden:\n  extra_apt_packages:\n    - mariadb-client\n    - postgresql-client\n"
        ),
    )
    cfg = load_config(repo / ".jailbee" / "config.yaml")
    assert cfg.golden.extra_apt_packages == ["mariadb-client", "postgresql-client"]


@pytest.mark.parametrize(
    "bad_pkg",
    [
        "bad pkg",  # whitespace
        "pkg;rm -rf /",  # shell metachar
        "pkg$(whoami)",  # command substitution
        "",  # empty
        "Pkg",  # uppercase not allowed in debian package names
    ],
)
def test_golden_extra_apt_packages_invalid_name_raises(tmp_path, mocker, bad_pkg):
    mocker.patch("jailbee.config.detect_default_branch", return_value="main")
    repo = _write_repo(
        tmp_path,
        name="myrepo",
        config_yaml=f'golden:\n  extra_apt_packages:\n    - "{bad_pkg}"\n',
    )
    with pytest.raises(ConfigError):
        load_config(repo / ".jailbee" / "config.yaml")


# ---------- shared_caches


def test_shared_caches_default_is_ssh_only(tmp_path, mocker):
    """`_default_shared_caches()` is stack-neutral (ssh only) — language
    caches (pnpm/gradle/npm/m2) are opt-in via `shared_caches:`. It also
    no longer carries claude or jetbrains entries — those live in
    `effective_shared_caches()` driven by `claude.enabled` and
    `jetbrains.enabled` respectively."""
    mocker.patch("jailbee.config.detect_default_branch", return_value="main")
    repo = _write_repo(tmp_path, name="myrepo")
    cfg = load_config(repo / ".jailbee" / "config.yaml")
    names = [c.name for c in cfg.shared_caches]
    assert names == ["ssh"]


def test_shared_caches_empty_override(tmp_path, mocker):
    mocker.patch("jailbee.config.detect_default_branch", return_value="main")
    repo = _write_repo(tmp_path, name="myrepo", config_yaml="shared_caches: []\n")
    cfg = load_config(repo / ".jailbee" / "config.yaml")
    assert cfg.shared_caches == []


def test_shared_caches_invalid_name_raises(tmp_path, mocker):
    mocker.patch("jailbee.config.detect_default_branch", return_value="main")
    repo = _write_repo(
        tmp_path,
        name="myrepo",
        config_yaml=(
            "shared_caches:\n"
            "  - name: Invalid_Name\n"
            "    host_subpath: foo\n"
            "    container_path: ~/foo\n"
        ),
    )
    with pytest.raises(ConfigError, match="name"):
        load_config(repo / ".jailbee" / "config.yaml")


def test_shared_caches_duplicate_name_raises(tmp_path, mocker):
    mocker.patch("jailbee.config.detect_default_branch", return_value="main")
    repo = _write_repo(
        tmp_path,
        name="myrepo",
        config_yaml=(
            "shared_caches:\n"
            "  - {name: a, host_subpath: a1, container_path: ~/a1}\n"
            "  - {name: a, host_subpath: a2, container_path: ~/a2}\n"
        ),
    )
    with pytest.raises(ConfigError, match="duplicate"):
        load_config(repo / ".jailbee" / "config.yaml")


def test_shared_caches_relative_container_path_raises(tmp_path, mocker):
    mocker.patch("jailbee.config.detect_default_branch", return_value="main")
    repo = _write_repo(
        tmp_path,
        name="myrepo",
        config_yaml=(
            "shared_caches:\n"
            "  - name: bad\n"
            "    host_subpath: x\n"
            "    container_path: relative/path\n"
        ),
    )
    with pytest.raises(ConfigError, match="container_path"):
        load_config(repo / ".jailbee" / "config.yaml")


# ---------- autostart step list


def test_autostart_step_minimal_fields(tmp_path, mocker):
    mocker.patch("jailbee.config.detect_default_branch", return_value="main")
    repo = _write_repo(
        tmp_path,
        name="myrepo",
        config_yaml=("autostart:\n  on_create:\n    - {name: build, run: 'make build'}\n"),
    )
    cfg = load_config(repo / ".jailbee" / "config.yaml")
    assert len(cfg.autostart.on_create) == 1
    step = cfg.autostart.on_create[0]
    assert step.name == "build"
    assert step.run == "make build"
    assert step.network is None
    assert step.mounts == []
    assert step.background is False
    assert step.continue_on_error is False
    assert step.timeout is None


def test_autostart_unknown_mount_caught_by_validate_runtime(tmp_path, mocker):
    mocker.patch("jailbee.config.detect_default_branch", return_value="main")
    repo = _write_repo(
        tmp_path,
        name="myrepo",
        config_yaml=(
            "autostart:\n  on_create:\n    - {name: a, run: 'echo x', mounts: [nonexistent]}\n"
        ),
    )
    cfg = load_config(repo / ".jailbee" / "config.yaml")
    issues = cfg.validate_runtime()
    assert any("nonexistent" in i for i in issues)


def test_autostart_duplicate_step_names_within_trigger_raises(tmp_path, mocker):
    mocker.patch("jailbee.config.detect_default_branch", return_value="main")
    repo = _write_repo(
        tmp_path,
        name="myrepo",
        config_yaml=(
            "autostart:\n  on_create:\n    - {name: x, run: 'a'}\n    - {name: x, run: 'b'}\n"
        ),
    )
    with pytest.raises(ConfigError, match="duplicate"):
        load_config(repo / ".jailbee" / "config.yaml")


def test_autostart_block_defaults(tmp_path, mocker):
    mocker.patch("jailbee.config.detect_default_branch", return_value="main")
    repo = _write_repo(tmp_path, name="myrepo")
    cfg = load_config(repo / ".jailbee" / "config.yaml")
    assert cfg.jetbrains.autostart is False
    assert cfg.chrome.autostart is False
    assert cfg.autostart.env == {}
    assert cfg.chrome.url is None


# --- container.env (repo-wide container env vars) ---


def test_container_env_defaults_empty(tmp_path, mocker):
    mocker.patch("jailbee.config.detect_default_branch", return_value="main")
    repo = _write_repo(tmp_path, name="myrepo")
    cfg = load_config(repo / ".jailbee" / "config.yaml")
    assert cfg.container.env == {}


def test_container_env_loaded_from_yaml(tmp_path, mocker):
    mocker.patch("jailbee.config.detect_default_branch", return_value="main")
    repo = _write_repo(
        tmp_path,
        name="myrepo",
        config_yaml=(
            "container:\n  env:\n    FOO: bar\n    NODE_OPTIONS: '--max-old-space-size=4096'\n"
        ),
    )
    cfg = load_config(repo / ".jailbee" / "config.yaml")
    assert cfg.container.env == {"FOO": "bar", "NODE_OPTIONS": "--max-old-space-size=4096"}


def test_container_env_rejects_invalid_name(tmp_path, mocker):
    mocker.patch("jailbee.config.detect_default_branch", return_value="main")
    repo = _write_repo(
        tmp_path,
        name="myrepo",
        config_yaml="container:\n  env:\n    '1BAD': x\n",
    )
    with pytest.raises(ConfigError, match="invalid env var name"):
        load_config(repo / ".jailbee" / "config.yaml")


def test_retired_open_chrome_string_rejected_with_migration_message(tmp_path, mocker):
    """Retired `autostart.open_chrome: "<url>"` must fail with a clear
    migration pointer to the new `chrome.url` field."""
    mocker.patch("jailbee.config.detect_default_branch", return_value="main")
    repo = _write_repo(
        tmp_path,
        name="myrepo",
        config_yaml="autostart:\n  open_chrome: https://example.com\n",
    )
    with pytest.raises(ConfigError, match=r"chrome\.autostart"):
        load_config(repo / ".jailbee" / "config.yaml")


# --- egress_allow validation ---


def test_egress_allow_accepts_valid_entries():
    cfg_dict = {
        "egress_allow": [
            "github.com",
            "github.com:22",
            "10.0.0.0/8",
            "10.0.0.0/8:5432",
            "1.2.3.4",
            "1.2.3.4:80",
        ],
    }
    cfg = Config.model_validate(cfg_dict)
    assert len(cfg.egress_allow) == 6


def test_egress_allow_rejects_bad_port():
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="port"):
        Config.model_validate({"egress_allow": ["github.com:99999"]})


def test_egress_allow_rejects_non_numeric_port():
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="port"):
        Config.model_validate({"egress_allow": ["github.com:abc"]})


def test_egress_allow_rejects_garbage_host():
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="host"):
        Config.model_validate({"egress_allow": ["not a valid host!"]})


def test_egress_allow_rejects_empty_string():
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="empty"):
        Config.model_validate({"egress_allow": [""]})


def test_obsolete_networks_key_rejected():
    """The pre-flat `networks:` key no longer exists in the schema."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError) as exc_info:
        Config.model_validate(
            {"networks": {"strict": {"egress_allow": ["github.com"]}}},
        )
    msg = str(exc_info.value)
    assert "extra_forbidden" in msg
    assert "networks" in msg


def test_jetbrains_userprefs_from_host_defaults_to_false(tmp_path, mocker):
    """Default false because license-host egress is no longer gated on
    this flag; users who want to share host JBA tokens opt in
    explicitly via YAML."""
    mocker.patch("jailbee.config.detect_default_branch", return_value="main")
    cfg_path = _make_config(tmp_path, "{}\n")

    cfg = load_config(cfg_path)

    assert cfg.jetbrains.userprefs_from_host is False


def test_jetbrains_userprefs_from_host_can_be_enabled(tmp_path, mocker):
    """Opt-in via YAML for users who do want host token sharing."""
    mocker.patch("jailbee.config.detect_default_branch", return_value="main")
    cfg_path = _make_config(tmp_path, "jetbrains:\n  userprefs_from_host: true\n")

    cfg = load_config(cfg_path)

    assert cfg.jetbrains.userprefs_from_host is True


def test_validate_runtime_reports_missing_jetbrains_userprefs(tmp_path, mocker):
    """When jetbrains is enabled and userprefs_from_host=true is opted
    in but the host source dir doesn't exist, validate_runtime flags
    it."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    fake_home = tmp_path / "fake-home"
    fake_home.mkdir()
    mocker.patch("jailbee.config.Path.home", return_value=fake_home)

    cfg = _make_runtime_cfg(tmp_path, repo_path=repo)
    object.__setattr__(cfg.jetbrains, "enabled", True)
    object.__setattr__(cfg.jetbrains, "userprefs_from_host", True)
    object.__setattr__(cfg.jetbrains, "toolbox_host_path", None)
    issues = cfg.validate_runtime()

    assert any("jetbrains.userprefs_from_host" in issue for issue in issues)


def test_validate_runtime_silent_when_jetbrains_userprefs_disabled(tmp_path, mocker):
    """If the user opts out, missing host dir is not a problem."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    fake_home = tmp_path / "fake-home"
    fake_home.mkdir()
    mocker.patch("jailbee.config.Path.home", return_value=fake_home)

    cfg = _make_runtime_cfg(tmp_path, repo_path=repo)
    object.__setattr__(cfg.jetbrains, "userprefs_from_host", False)
    object.__setattr__(cfg.jetbrains, "toolbox_host_path", None)
    issues = cfg.validate_runtime()

    assert not any("jetbrains" in issue.lower() for issue in issues)


def test_effective_egress_allow_adds_jetbrains_when_enabled():
    """jetbrains.enabled alone auto-appends the license/plugin/CDN
    hosts so the IDE can activate its account license regardless of
    where user prefs live."""
    from jailbee.config import JETBRAINS_LICENSE_HOSTS

    cfg = Config.model_validate(
        {
            "egress_allow": ["github.com"],
            "jetbrains": {"enabled": True},
        }
    )

    effective = cfg.effective_egress_allow()

    assert effective[0] == "github.com"
    for host in JETBRAINS_LICENSE_HOSTS:
        assert host in effective


def test_effective_egress_allow_adds_jetbrains_without_userprefs_from_host():
    """Egress is gated only on `enabled`. Even with
    userprefs_from_host=false the IDE still needs license-server access
    in strict mode."""
    from jailbee.config import JETBRAINS_LICENSE_HOSTS

    cfg = Config.model_validate(
        {
            "egress_allow": ["github.com"],
            "jetbrains": {"enabled": True, "userprefs_from_host": False},
        }
    )

    effective = cfg.effective_egress_allow()
    for host in JETBRAINS_LICENSE_HOSTS:
        assert host in effective


def test_effective_egress_allow_omits_jetbrains_when_disabled():
    """jetbrains.enabled=false suppresses all auto-additions, even if
    userprefs_from_host or ai_enabled are true."""
    cfg = Config.model_validate(
        {
            "egress_allow": ["github.com"],
            "jetbrains": {
                "enabled": False,
                "userprefs_from_host": True,
                "ai_enabled": True,
            },
        }
    )

    assert cfg.effective_egress_allow() == ["github.com"]


def test_effective_egress_allow_omits_ai_hosts_by_default():
    """AI hosts are opt-in; enabling jetbrains alone does not add them."""
    from jailbee.config import JETBRAINS_AI_HOSTS

    cfg = Config.model_validate({"egress_allow": ["github.com"], "jetbrains": {"enabled": True}})

    effective = cfg.effective_egress_allow()
    for host in JETBRAINS_AI_HOSTS:
        assert host not in effective


def test_effective_egress_allow_adds_ai_hosts_when_ai_enabled():
    """jetbrains.enabled + ai_enabled auto-appends AI Assistant hosts."""
    from jailbee.config import JETBRAINS_AI_HOSTS

    cfg = Config.model_validate(
        {
            "egress_allow": ["github.com"],
            "jetbrains": {"enabled": True, "ai_enabled": True},
        }
    )

    effective = cfg.effective_egress_allow()
    for host in JETBRAINS_AI_HOSTS:
        assert host in effective


def test_effective_egress_allow_dedupes_user_entries():
    """If user already lists a jetbrains host, no duplicate is added."""
    from jailbee.config import JETBRAINS_LICENSE_HOSTS

    user_entry = JETBRAINS_LICENSE_HOSTS[0]
    cfg = Config.model_validate({"egress_allow": [user_entry], "jetbrains": {"enabled": True}})

    effective = cfg.effective_egress_allow()

    assert effective.count(user_entry) == 1


# --- jetbrains.ide + jetbrains.autostart (PyCharm/JetBrains support) ---


def test_jetbrains_ide_defaults_to_idea(tmp_path, mocker):
    mocker.patch("jailbee.config.detect_default_branch", return_value="main")
    repo = _write_repo(tmp_path, name="myrepo")
    cfg = load_config(repo / ".jailbee" / "config.yaml")
    assert cfg.jetbrains.ide == "idea"


@pytest.mark.parametrize(
    "ide_name",
    [
        "idea",
        "webstorm",
        "pycharm",
        "goland",
        "clion",
        "phpstorm",
        "rider",
        "rubymine",
        "datagrip",
        "rustrover",
        "aqua",
        "dataspell",
    ],
)
def test_jetbrains_ide_accepts_all_supported_jetbrains_ides(ide_name):
    cfg = Config.model_validate({"jetbrains": {"ide": ide_name}})
    assert cfg.jetbrains.ide == ide_name


def test_jetbrains_ide_rejects_unknown_value():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Config.model_validate({"jetbrains": {"ide": "vim"}})


def test_jetbrains_autostart_default_false(tmp_path, mocker):
    mocker.patch("jailbee.config.detect_default_branch", return_value="main")
    repo = _write_repo(tmp_path, name="myrepo")
    cfg = load_config(repo / ".jailbee" / "config.yaml")
    assert cfg.jetbrains.autostart is False


def test_jetbrains_autostart_accepts_explicit_false():
    cfg = Config.model_validate({"jetbrains": {"autostart": False}})
    assert cfg.jetbrains.autostart is False


# ---------- docker_registry_mirror.extra_registries (per-repo, issue follow-up)


def test_docker_registry_mirror_defaults_to_empty_extra_registries(tmp_path, mocker):
    mocker.patch("jailbee.config.detect_default_branch", return_value="main")
    cfg_path = _make_config(tmp_path, "{}\n")
    cfg = load_config(cfg_path)
    assert cfg.docker_registry_mirror.extra_registries == []


def test_docker_registry_mirror_extra_registries_loaded_from_yaml(tmp_path, mocker):
    mocker.patch("jailbee.config.detect_default_branch", return_value="main")
    cfg_path = _make_config(
        tmp_path,
        "docker_registry_mirror:\n"
        "  extra_registries:\n"
        "    - 803520778560.dkr.ecr.eu-north-1.amazonaws.com\n"
        "    - my-private.example.com\n",
    )
    cfg = load_config(cfg_path)
    assert cfg.docker_registry_mirror.extra_registries == [
        "803520778560.dkr.ecr.eu-north-1.amazonaws.com",
        "my-private.example.com",
    ]


def test_docker_registry_mirror_extra_registries_rejects_empty_string():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Config.model_validate({"docker_registry_mirror": {"extra_registries": [""]}})


def test_docker_registry_mirror_extra_registries_rejects_whitespace_in_hostname():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Config.model_validate({"docker_registry_mirror": {"extra_registries": ["foo bar.com"]}})


def test_docker_registry_mirror_extra_registries_rejects_scheme():
    """rpardini's REGISTRIES is space-separated hostnames; including a scheme
    breaks its nginx upstream generation."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Config.model_validate(
            {"docker_registry_mirror": {"extra_registries": ["https://example.com"]}}
        )


def test_docker_registry_mirror_extra_registries_rejects_path():
    """Paths/refs aren't registry hostnames."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Config.model_validate({"docker_registry_mirror": {"extra_registries": ["example.com/foo"]}})


def test_docker_registry_mirror_extra_registries_accepts_host_port():
    """Custom-port mirrors (host:5000) are legitimate."""
    cfg = Config.model_validate(
        {"docker_registry_mirror": {"extra_registries": ["registry.local:5000"]}}
    )
    assert cfg.docker_registry_mirror.extra_registries == ["registry.local:5000"]


def test_docker_registry_mirror_unknown_key_rejected():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Config.model_validate({"docker_registry_mirror": {"nope": True}})


# --- Phase B Task 1: golden.disable_snippets + reserved env keys -------------


def test_golden_disable_snippets_defaults_to_empty(tmp_path, mocker):
    """disable_snippets defaults to [] and accepts a list of names."""
    mocker.patch("jailbee.config.detect_default_branch", return_value="main")
    repo = tmp_path / "r"
    (repo / ".jailbee").mkdir(parents=True)
    (repo / ".git").mkdir()
    (repo / ".jailbee" / "config.yaml").write_text("golden:\n  disable_snippets: ['70-claude']\n")

    from jailbee.config import load_config

    cfg = load_config(repo / ".jailbee" / "config.yaml")

    assert cfg.golden.disable_snippets == ["70-claude"]


def test_golden_enable_snippets_defaults_empty_and_parses(tmp_path):
    from jailbee.config import Golden

    assert Golden().enable_snippets == []
    g = Golden(enable_snippets=["nodejs", "docker"])
    assert g.enable_snippets == ["nodejs", "docker"]


def test_provision_env_rejects_gie_reserved_keys(tmp_path, mocker):
    """JAILBEE_USER_HOME and JAILBEE_PROVISION_DIR may not be shadowed."""
    mocker.patch("jailbee.config.detect_default_branch", return_value="main")
    repo = tmp_path / "r"
    (repo / ".jailbee").mkdir(parents=True)
    (repo / ".git").mkdir()
    (repo / ".jailbee" / "config.yaml").write_text(
        "golden:\n  provision_env:\n    JAILBEE_USER_HOME: /tmp/whatever\n"
    )

    from jailbee.config import ConfigError, load_config

    with pytest.raises(ConfigError) as exc:
        load_config(repo / ".jailbee" / "config.yaml")

    assert "JAILBEE_USER_HOME" in str(exc.value)


# ---------- GpgConfig model ----------


def test_gpg_config_defaults():
    from jailbee.config import GpgConfig

    cfg = GpgConfig()
    assert cfg.enabled is False


def test_gpg_config_rejects_extra_keys():
    from pydantic import ValidationError

    from jailbee.config import GpgConfig

    with pytest.raises(ValidationError):
        GpgConfig(enabled=True, unknown="x")  # type: ignore[call-arg]


# ---------- SshConfig model ----------


def test_ssh_config_defaults():
    from jailbee.config import SshConfig

    cfg = SshConfig()
    assert cfg.enabled is False
    assert cfg.seed_from_host is True


def test_ssh_config_can_disable_seed():
    from jailbee.config import SshConfig

    cfg = SshConfig(enabled=True, seed_from_host=False)
    assert cfg.seed_from_host is False


# ---------- JetbrainsConfig model ----------


def test_jetbrains_config_defaults():
    from jailbee.config import JetbrainsConfig

    cfg = JetbrainsConfig()
    assert cfg.ide == "idea"
    assert cfg.userprefs_from_host is False
    assert cfg.ai_enabled is False
    assert cfg.autostart is False
    assert cfg.toolbox_host_path == (Path.home() / ".local/share/JetBrains/Toolbox")


def test_jetbrains_config_accepts_null_toolbox():
    from jailbee.config import JetbrainsConfig

    cfg = JetbrainsConfig(toolbox_host_path=None)
    assert cfg.toolbox_host_path is None


def test_jetbrains_config_rejects_unknown_ide():
    from pydantic import ValidationError

    from jailbee.config import JetbrainsConfig

    with pytest.raises(ValidationError):
        JetbrainsConfig(ide="notepad")  # type: ignore[arg-type]


# ---------- ChromeConfig model ----------


def test_chrome_config_defaults():
    from jailbee.config import ChromeConfig

    cfg = ChromeConfig()
    assert cfg.url is None
    assert cfg.dark_mode is False
    assert cfg.autostart is False


def test_chrome_config_accepts_url_and_dark_mode():
    from jailbee.config import ChromeConfig

    cfg = ChromeConfig(url="https://app.example.com", dark_mode=True, autostart=False)
    assert cfg.url == "https://app.example.com"
    assert cfg.dark_mode is True
    assert cfg.autostart is False


def test_chrome_config_enabled_default_false():
    from jailbee.config import ChromeConfig

    assert ChromeConfig().enabled is False


def test_chrome_config_enabled_can_be_enabled():
    from jailbee.config import ChromeConfig

    assert ChromeConfig(enabled=True).enabled is True


def test_jetbrains_config_enabled_default_false():
    from jailbee.config import JetbrainsConfig

    assert JetbrainsConfig().enabled is False


def test_jetbrains_config_enabled_can_be_enabled():
    from jailbee.config import JetbrainsConfig

    assert JetbrainsConfig(enabled=True).enabled is True


def test_claude_config_enabled_default_false():
    from jailbee.config import ClaudeAgentConfig

    assert ClaudeAgentConfig().enabled is False


def test_claude_config_can_be_enabled():
    from jailbee.config import ClaudeAgentConfig

    cfg = ClaudeAgentConfig(enabled=True)
    assert cfg.enabled is True


def test_claude_config_extra_keys_rejected():
    from pydantic import ValidationError

    from jailbee.config import ClaudeAgentConfig

    with pytest.raises(ValidationError):
        ClaudeAgentConfig(unknown_field=True)  # type: ignore[call-arg]


def test_config_has_claude_block_disabled_by_default(tmp_path, mocker):
    """Config.claude defaults to a disabled ClaudeAgentConfig."""
    mocker.patch("jailbee.config.detect_default_branch", return_value="main")
    cfg_path = _make_config(tmp_path, "{}\n")
    cfg = load_config(cfg_path)
    assert cfg.claude.enabled is False


def test_claude_config_autostart_defaults():
    from jailbee.config import ClaudeAgentConfig

    # `command` on the bare model defaults to "" (inherited from AgentConfig) —
    # "claude" is supplied by `Config.claude`'s fallback construction and by
    # `claude_preset()` on the load path, not by the model itself.
    cfg = ClaudeAgentConfig()
    assert cfg.autostart is False
    assert cfg.command == ""


def test_claude_config_autostart_accepts_custom_command():
    from jailbee.config import ClaudeAgentConfig

    cfg = ClaudeAgentConfig(
        enabled=True, autostart=True, command="claude --dangerously-skip-permissions"
    )
    assert cfg.autostart is True
    assert cfg.command == "claude --dangerously-skip-permissions"


def test_validate_runtime_rejects_autostart_without_enabled(tmp_path):
    """Exactly one diagnostic for this misconfiguration.

    `resolve_agents_raw` always populates `agents["claude"]`, so a legacy
    claude-specific special case alongside the generic per-agent loop would
    double-report the same issue. `validate_runtime`'s generic loop is the
    single source; guard against a duplicate creeping back in with a count
    assertion rather than `any(...)`, which can't see a duplicate.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = _make_runtime_cfg(tmp_path, repo_path=repo)
    cfg = with_agent(cfg, "claude", autostart=True)
    issues = cfg.validate_runtime()
    matches = [
        i for i in issues if "agents.claude.autostart=true requires agents.claude.enabled=true" in i
    ]
    assert len(matches) == 1
    assert "shared ~/.claude mount and Anthropic egress are gated" in matches[0]


def test_validate_runtime_silent_when_autostart_and_enabled(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = _make_runtime_cfg(tmp_path, repo_path=repo)
    cfg = with_agent(cfg, "claude", enabled=True, autostart=True)
    issues = cfg.validate_runtime()
    matches = [i for i in issues if "claude.autostart" in i]
    assert len(matches) == 0


def test_claude_auto_update_defaults_true(tmp_path, mocker):
    """`claude.auto_update` defaults to True (auto-update existing installs)."""
    mocker.patch("jailbee.config.detect_default_branch", return_value="main")
    repo = _write_repo(tmp_path, name="myrepo")
    cfg = load_config(repo / ".jailbee" / "config.yaml")
    assert cfg.claude.auto_update is True


def test_claude_auto_update_can_be_disabled(tmp_path, mocker):
    """`claude.auto_update: false` parses and overrides the default."""
    mocker.patch("jailbee.config.detect_default_branch", return_value="main")
    repo = _write_repo(
        tmp_path,
        name="myrepo",
        config_yaml="claude:\n  enabled: true\n  auto_update: false\n",
    )
    cfg = load_config(repo / ".jailbee" / "config.yaml")
    assert cfg.claude.auto_update is False


def test_claude_install_jailbee_skills_defaults_true_via_load_config(tmp_path, mocker):
    """`claude.install_jailbee_skills` round-trips through YAML loading."""
    mocker.patch("jailbee.config.detect_default_branch", return_value="main")
    repo = _write_repo(tmp_path, name="myrepo")
    cfg = load_config(repo / ".jailbee" / "config.yaml")
    assert cfg.claude.install_jailbee_skills is True


def test_claude_install_jailbee_skills_override_false_via_yaml(tmp_path, mocker):
    """`claude.install_jailbee_skills: false` parses and overrides the default."""
    mocker.patch("jailbee.config.detect_default_branch", return_value="main")
    repo = _write_repo(
        tmp_path,
        name="myrepo",
        config_yaml="claude:\n  enabled: true\n  install_jailbee_skills: false\n",
    )
    cfg = load_config(repo / ".jailbee" / "config.yaml")
    assert cfg.claude.install_jailbee_skills is False


def test_claude_pr_prompt_defaults_to_none(tmp_path, mocker):
    """No `claude.pr_prompt` means jailbee's own prompt is used unchanged."""
    mocker.patch("jailbee.config.detect_default_branch", return_value="main")
    repo = _write_repo(tmp_path, name="myrepo")
    cfg = load_config(repo / ".jailbee" / "config.yaml")
    assert cfg.claude.pr_prompt is None


def test_claude_pr_prompt_reads_a_multiline_block_from_repo_yaml(tmp_path, mocker):
    """A repo encodes its PR standard as a YAML block scalar."""
    mocker.patch("jailbee.config.detect_default_branch", return_value="main")
    repo = _write_repo(
        tmp_path,
        name="myrepo",
        config_yaml=(
            "claude:\n"
            "  enabled: true\n"
            "  pr_prompt: |\n"
            "    Use these headings:\n"
            "    ## Motivation\n"
            "    ## Risk\n"
        ),
    )
    cfg = load_config(repo / ".jailbee" / "config.yaml")
    assert cfg.claude.pr_prompt == "Use these headings:\n## Motivation\n## Risk\n"


def test_claude_pr_prompt_rejects_an_oversized_value():
    """A pathological value fails loudly at load instead of inside the container."""
    from pydantic import ValidationError

    from jailbee.config import ClaudeAgentConfig

    with pytest.raises(ValidationError):
        ClaudeAgentConfig(enabled=True, pr_prompt="x" * 20_001)


def test_claude_ai_pr_model_defaults_to_sonnet(tmp_path, mocker):
    """PR text is a bounded summarisation job — it does not need the Opus default."""
    mocker.patch("jailbee.config.detect_default_branch", return_value="main")
    repo = _write_repo(tmp_path, name="myrepo")
    cfg = load_config(repo / ".jailbee" / "config.yaml")
    assert cfg.claude.ai_pr_model == "sonnet"


def test_claude_ai_pr_model_accepts_a_pinned_model_id(tmp_path, mocker):
    """The value passes through to `claude --model`, so full IDs must survive."""
    mocker.patch("jailbee.config.detect_default_branch", return_value="main")
    repo = _write_repo(
        tmp_path,
        name="myrepo",
        config_yaml="claude:\n  enabled: true\n  ai_pr_model: claude-haiku-4-5\n",
    )
    cfg = load_config(repo / ".jailbee" / "config.yaml")
    assert cfg.claude.ai_pr_model == "claude-haiku-4-5"


def test_claude_ai_pr_model_null_inherits_the_container_default(tmp_path, mocker):
    mocker.patch("jailbee.config.detect_default_branch", return_value="main")
    repo = _write_repo(
        tmp_path,
        name="myrepo",
        config_yaml="claude:\n  enabled: true\n  ai_pr_model: null\n",
    )
    cfg = load_config(repo / ".jailbee" / "config.yaml")
    assert cfg.claude.ai_pr_model is None


def test_claude_ai_pr_model_rejects_extra_flags():
    """A model name never contains whitespace; smuggling flags in must not work."""
    from pydantic import ValidationError

    from jailbee.config import ClaudeAgentConfig

    with pytest.raises(ValidationError, match="ai_pr_model"):
        ClaudeAgentConfig(enabled=True, ai_pr_model="sonnet --dangerously-skip-permissions")


def test_claude_ai_pr_model_rejects_an_empty_string():
    """Empty means "inherit the container default" — spell that `null`, not ''."""
    from pydantic import ValidationError

    from jailbee.config import ClaudeAgentConfig

    with pytest.raises(ValidationError, match="ai_pr_model"):
        ClaudeAgentConfig(enabled=True, ai_pr_model="   ")


def test_claude_ai_pr_timeout_defaults_to_600(tmp_path, mocker):
    """Generation is an agentic run: 129s in this repo on a 21-file diff.

    The old hard-coded 180s left no room for a larger tree, and there was no
    config key to raise it.
    """
    mocker.patch("jailbee.config.detect_default_branch", return_value="main")
    repo = _write_repo(tmp_path, name="myrepo")
    cfg = load_config(repo / ".jailbee" / "config.yaml")
    assert cfg.claude.ai_pr_timeout == 600


def test_claude_ai_pr_timeout_is_raisable_from_repo_yaml(tmp_path, mocker):
    mocker.patch("jailbee.config.detect_default_branch", return_value="main")
    repo = _write_repo(
        tmp_path,
        name="myrepo",
        config_yaml="claude:\n  enabled: true\n  ai_pr_timeout: 1200\n",
    )
    cfg = load_config(repo / ".jailbee" / "config.yaml")
    assert cfg.claude.ai_pr_timeout == 1200


@pytest.mark.parametrize("bad", [0, -30])
def test_claude_ai_pr_timeout_rejects_non_positive_values(bad):
    """`timeout=0` expires instantly — a config error, not a way to disable AI.

    Turning generation off is `ai_pr_description: false`.
    """
    from pydantic import ValidationError

    from jailbee.config import ClaudeAgentConfig

    with pytest.raises(ValidationError, match="ai_pr_timeout"):
        ClaudeAgentConfig(enabled=True, ai_pr_timeout=bad)


def test_config_rejects_the_retired_install_gie_skills_key(tmp_path, mocker):
    """End-to-end, because which error surfaces depends on ordering.

    `ClaudeAgentConfig` forbids extras, so a config carrying the pre-1.0 key name
    fails either way — but on pydantic's "Extra inputs are not permitted",
    which names neither the new key nor the fix. `_check_retired_keys` has to
    run first for the user to be told what to rename it to.
    """
    mocker.patch("jailbee.config.detect_default_branch", return_value="main")
    repo = tmp_path / "r"
    (repo / ".jailbee").mkdir(parents=True)
    (repo / ".git").mkdir()
    (repo / ".jailbee" / "config.yaml").write_text("claude:\n  install_gie_skills: false\n")

    with pytest.raises(ConfigError) as exc:
        load_config(repo / ".jailbee" / "config.yaml")

    msg = str(exc.value)
    assert "claude.install_jailbee_skills" in msg
    assert "Extra inputs" not in msg


def test_config_rejects_the_retired_install_gie_skills_key_via_agents_claude(tmp_path, mocker):
    """Same retired-key check, `agents.claude` spelling.

    `_check_retired_keys` runs on the raw dict before `Config.model_validate`,
    so it must catch this under `agents.claude` too — not just the legacy
    top-level `claude:` block — or a user who has already migrated to the
    new spelling loses the friendly rename message and falls through to
    pydantic's "Extra inputs are not permitted" instead.
    """
    mocker.patch("jailbee.config.detect_default_branch", return_value="main")
    repo = tmp_path / "r"
    (repo / ".jailbee").mkdir(parents=True)
    (repo / ".git").mkdir()
    (repo / ".jailbee" / "config.yaml").write_text(
        "agents:\n  claude:\n    install_gie_skills: false\n"
    )

    with pytest.raises(ConfigError) as exc:
        load_config(repo / ".jailbee" / "config.yaml")

    msg = str(exc.value)
    assert "claude.install_jailbee_skills" in msg
    assert "Extra inputs" not in msg


def test_claude_install_jailbee_skills_defaults_true():
    from jailbee.config import ClaudeAgentConfig

    assert ClaudeAgentConfig().install_jailbee_skills is True


def test_claude_install_jailbee_skills_override_by_keyword():
    from jailbee.config import ClaudeAgentConfig

    assert ClaudeAgentConfig(install_jailbee_skills=False).install_jailbee_skills is False


def test_claude_rejects_unknown_key(tmp_path, mocker):
    """ClaudeAgentConfig keeps extra='forbid' — unknown keys still raise."""
    mocker.patch("jailbee.config.detect_default_branch", return_value="main")
    repo = _write_repo(
        tmp_path,
        name="myrepo",
        config_yaml="claude:\n  enabled: true\n  bogus: 1\n",
    )
    with pytest.raises(ConfigError):
        load_config(repo / ".jailbee" / "config.yaml")


# ---------- _check_retired_keys ----------


@pytest.mark.parametrize(
    "key,parent,target",
    [
        ("ide", None, "jetbrains.ide"),
        ("chrome_url", None, "chrome.url"),
        ("seed_ssh_from_host", None, "ssh.seed_from_host"),
        ("jetbrains_userprefs_from_host", None, "jetbrains.userprefs_from_host"),
        ("open_ide", "autostart", "jetbrains.ide + jetbrains.autostart"),
        ("open_chrome", "autostart", "chrome.autostart"),
        ("chrome_dark_mode", "autostart", "chrome.dark_mode"),
        ("install_gie_skills", "claude", "claude.install_jailbee_skills"),
    ],
)
def test_retired_keys_raise_with_new_location(key, parent, target):
    from jailbee.config import _check_retired_keys

    raw: dict = {parent: {key: True}} if parent else {key: True}
    with pytest.raises(ConfigError) as exc:
        _check_retired_keys(raw)
    msg = str(exc.value)
    assert key in msg
    assert target in msg


def test_no_retired_keys_returns_none():
    from jailbee.config import _check_retired_keys

    _check_retired_keys({"container_user": {"uid": 1000}, "autostart": {"on_create": []}})


# ---------- Config wiring of new blocks ----------


def test_config_has_new_blocks_with_defaults(tmp_path, mocker):
    mocker.patch("jailbee.config.detect_default_branch", return_value="main")
    repo = tmp_path / "r"
    (repo / ".jailbee").mkdir(parents=True)
    (repo / ".git").mkdir()
    (repo / ".jailbee" / "config.yaml").write_text("{}\n")
    cfg = load_config(repo / ".jailbee" / "config.yaml")
    # Master switches default to False — every host-tooling integration
    # is opt-in. Sub-fields still carry their post-enable defaults so
    # `enabled: true` alone is enough to get the expected behaviour.
    assert cfg.gpg.enabled is False
    assert cfg.ssh.enabled is False
    assert cfg.ssh.seed_from_host is True
    assert cfg.jetbrains.enabled is False
    assert cfg.jetbrains.ide == "idea"
    assert cfg.jetbrains.userprefs_from_host is False
    assert cfg.jetbrains.ai_enabled is False
    assert cfg.jetbrains.autostart is False
    assert cfg.chrome.enabled is False
    assert cfg.chrome.url is None
    assert cfg.chrome.autostart is False


def test_config_rejects_retired_top_level_keys(tmp_path, mocker):
    mocker.patch("jailbee.config.detect_default_branch", return_value="main")
    repo = tmp_path / "r"
    (repo / ".jailbee").mkdir(parents=True)
    (repo / ".git").mkdir()
    (repo / ".jailbee" / "config.yaml").write_text("ide: pycharm\n")
    with pytest.raises(ConfigError) as exc:
        load_config(repo / ".jailbee" / "config.yaml")
    assert "jetbrains.ide" in str(exc.value)


def test_config_rejects_removed_seed_claude_from_host(tmp_path, mocker):
    """Old top-level key `seed_claude_from_host` is now a removed feature,
    not just a renamed one. Error must explain that the host-seed behaviour
    is gone — not redirect to a key that itself errors."""
    mocker.patch("jailbee.config.detect_default_branch", return_value="main")
    repo = tmp_path / "r"
    (repo / ".jailbee").mkdir(parents=True)
    (repo / ".git").mkdir()
    (repo / ".jailbee" / "config.yaml").write_text("seed_claude_from_host: true\n")
    with pytest.raises(ConfigError) as exc:
        load_config(repo / ".jailbee" / "config.yaml")
    assert "has been removed" in str(exc.value)


def test_config_rejects_removed_claude_seed_from_host_nested(tmp_path, mocker):
    """Same removal message when the user puts the key in the nested
    `claude:` block rather than at the top level."""
    mocker.patch("jailbee.config.detect_default_branch", return_value="main")
    repo = tmp_path / "r"
    (repo / ".jailbee").mkdir(parents=True)
    (repo / ".git").mkdir()
    (repo / ".jailbee" / "config.yaml").write_text("claude:\n  seed_from_host: true\n")
    with pytest.raises(ConfigError) as exc:
        load_config(repo / ".jailbee" / "config.yaml")
    assert "has been removed" in str(exc.value)


def test_config_rejects_removed_claude_seed_from_host_via_agents_claude(tmp_path, mocker):
    """Same removal message under the `agents.claude` spelling.

    `_check_retired_keys` checks both `raw["claude"]` and
    `raw["agents"]["claude"]` for `_REMOVED_KEYS_CLAUDE` — this exercises the
    second one specifically, so the removal message isn't silently lost for
    users who have already migrated off the legacy `claude:` block.
    """
    mocker.patch("jailbee.config.detect_default_branch", return_value="main")
    repo = tmp_path / "r"
    (repo / ".jailbee").mkdir(parents=True)
    (repo / ".git").mkdir()
    (repo / ".jailbee" / "config.yaml").write_text("agents:\n  claude:\n    seed_from_host: true\n")
    with pytest.raises(ConfigError) as exc:
        load_config(repo / ".jailbee" / "config.yaml")
    assert "has been removed" in str(exc.value)


def test_config_rejects_retired_autostart_keys(tmp_path, mocker):
    mocker.patch("jailbee.config.detect_default_branch", return_value="main")
    repo = tmp_path / "r"
    (repo / ".jailbee").mkdir(parents=True)
    (repo / ".git").mkdir()
    (repo / ".jailbee" / "config.yaml").write_text("autostart:\n  open_ide: false\n")
    with pytest.raises(ConfigError) as exc:
        load_config(repo / ".jailbee" / "config.yaml")
    assert "chrome.autostart" not in str(exc.value)
    assert "jetbrains.ide + jetbrains.autostart" in str(exc.value)


def test_effective_egress_allow_reads_new_location():
    from jailbee.config import JETBRAINS_LICENSE_HOSTS

    cfg = Config.model_validate(
        {
            "jetbrains": {"enabled": True, "userprefs_from_host": True},
            "egress_allow": ["github.com:22"],
        }
    )
    result = cfg.effective_egress_allow()
    for h in JETBRAINS_LICENSE_HOSTS:
        assert h in result
    assert "github.com:22" in result


def test_effective_egress_allow_omits_jetbrains_hosts_when_jetbrains_disabled():
    """jetbrains.enabled=false suppresses the auto-added license hosts even
    when userprefs_from_host is true. (The master switch is the only
    gate; sub-fields don't add anything on their own.)"""
    from jailbee.config import JETBRAINS_LICENSE_HOSTS

    cfg = Config.model_validate(
        {
            "jetbrains": {"enabled": False, "userprefs_from_host": True},
            "egress_allow": ["github.com:22"],
        }
    )
    result = cfg.effective_egress_allow()
    for h in JETBRAINS_LICENSE_HOSTS:
        assert h not in result
    assert result == ["github.com:22"]


def test_validate_runtime_skips_jetbrains_checks_when_disabled(tmp_path):
    """When jetbrains.enabled=false, validate_runtime ignores userprefs and
    toolbox host-path existence."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()

    cfg = Config.model_validate(
        {
            "jetbrains": {
                "enabled": False,
                "userprefs_from_host": True,
                "toolbox_host_path": "/nonexistent/Toolbox",
            },
        }
    )
    object.__setattr__(cfg, "repo_root", repo)
    object.__setattr__(cfg, "default_branch", "main")
    object.__setattr__(cfg, "container_prefix", "repo")
    object.__setattr__(cfg, "shared_dir", tmp_path / "shared")
    issues = cfg.validate_runtime()
    assert not any("jetbrains" in i.lower() for i in issues)


# ---------- TerminalKittyConfig model + autodetect ----------


def test_terminal_kitty_config_defaults():
    from jailbee.config import TerminalKittyConfig

    cfg = TerminalKittyConfig()
    assert cfg.enabled == "auto"
    assert cfg.host_terminfo_path is None


def test_terminal_kitty_config_accepts_explicit_path(tmp_path):
    from jailbee.config import TerminalKittyConfig

    f = tmp_path / "xterm-kitty"
    f.write_bytes(b"\x1a\x01")
    cfg = TerminalKittyConfig(enabled=True, host_terminfo_path=f)
    assert cfg.enabled is True
    assert cfg.host_terminfo_path == f


def test_terminal_kitty_config_extra_keys_rejected():
    from pydantic import ValidationError

    from jailbee.config import TerminalKittyConfig

    with pytest.raises(ValidationError):
        TerminalKittyConfig(unknown=True)  # type: ignore[call-arg]


def test_terminal_config_defaults():
    from jailbee.config import TerminalConfig, TerminalKittyConfig

    cfg = TerminalConfig()
    assert isinstance(cfg.kitty, TerminalKittyConfig)
    assert cfg.kitty.enabled == "auto"


def test_resolve_kitty_terminfo_explicit_wins(tmp_path):
    from jailbee.config import resolve_kitty_terminfo_path

    f = tmp_path / "explicit-xterm-kitty"
    f.write_bytes(b"\x1a\x01")
    assert resolve_kitty_terminfo_path(explicit=f) == f


def test_resolve_kitty_terminfo_explicit_missing_returns_none(tmp_path):
    from jailbee.config import resolve_kitty_terminfo_path

    missing = tmp_path / "nope"
    assert resolve_kitty_terminfo_path(explicit=missing) is None


def test_resolve_kitty_terminfo_autodetect_finds_first_existing(tmp_path, mocker):
    from jailbee.config import resolve_kitty_terminfo_path

    cand1 = tmp_path / "a/x/xterm-kitty"
    cand2 = tmp_path / "b/x/xterm-kitty"
    cand3 = tmp_path / "c/x/xterm-kitty"
    cand2.parent.mkdir(parents=True)
    cand2.write_bytes(b"\x1a\x01")

    mocker.patch(
        "jailbee.config._kitty_terminfo_candidates",
        return_value=[cand1, cand2, cand3],
    )

    assert resolve_kitty_terminfo_path(explicit=None) == cand2


def test_resolve_kitty_terminfo_autodetect_none_match(tmp_path, mocker):
    from jailbee.config import resolve_kitty_terminfo_path

    cand = tmp_path / "absent/xterm-kitty"
    mocker.patch(
        "jailbee.config._kitty_terminfo_candidates",
        return_value=[cand],
    )

    assert resolve_kitty_terminfo_path(explicit=None) is None


def test_kitty_terminfo_candidates_includes_known_paths():
    """Smoke-check the three candidate paths documented in the spec
    appear in the candidate list, in order."""
    from jailbee.config import _kitty_terminfo_candidates

    paths = [str(p) for p in _kitty_terminfo_candidates()]
    assert paths[0] == "/usr/share/terminfo/x/xterm-kitty"
    assert any(".local/kitty.app/lib/kitty/terminfo/x/xterm-kitty" in p for p in paths)
    assert any(".terminfo/x/xterm-kitty" in p for p in paths)


# ---------- terminal.kitty auto-mount ----------


def _kitty_mount(mounts):
    """Return the kitty auto-mount from a list, or None."""
    return next(
        (m for m in mounts if str(m.container) == "/usr/share/terminfo/x/xterm-kitty"),
        None,
    )


def test_kitty_auto_mount_present_when_enabled_true_and_path_exists(tmp_path, mocker):
    mocker.patch("jailbee.config.detect_default_branch", return_value="main")
    f = tmp_path / "xterm-kitty"
    f.write_bytes(b"\x1a\x01")
    cfg_path = _make_config(
        tmp_path,
        f"terminal:\n  kitty:\n    enabled: true\n    host_terminfo_path: {f}\n",
    )
    cfg = load_config(cfg_path)
    m = _kitty_mount(cfg.effective_host_mounts())
    assert m is not None
    assert m.host == f
    assert m.readonly is True


def test_kitty_auto_mount_absent_when_enabled_false(tmp_path, mocker):
    mocker.patch("jailbee.config.detect_default_branch", return_value="main")
    f = tmp_path / "xterm-kitty"
    f.write_bytes(b"\x1a\x01")
    cfg_path = _make_config(
        tmp_path,
        f"terminal:\n  kitty:\n    enabled: false\n    host_terminfo_path: {f}\n",
    )
    cfg = load_config(cfg_path)
    assert _kitty_mount(cfg.effective_host_mounts()) is None


def test_kitty_auto_mount_absent_when_auto_and_no_path(tmp_path, mocker):
    """enabled: auto with no resolvable path is a silent no-op."""
    mocker.patch("jailbee.config.detect_default_branch", return_value="main")
    mocker.patch(
        "jailbee.config._kitty_terminfo_candidates",
        return_value=[tmp_path / "nowhere"],
    )
    cfg_path = _make_config(tmp_path, "terminal:\n  kitty:\n    enabled: auto\n")
    cfg = load_config(cfg_path)
    assert _kitty_mount(cfg.effective_host_mounts()) is None


def test_kitty_auto_mount_present_when_auto_and_autodetect_succeeds(tmp_path, mocker):
    mocker.patch("jailbee.config.detect_default_branch", return_value="main")
    f = tmp_path / "candidate" / "xterm-kitty"
    f.parent.mkdir()
    f.write_bytes(b"\x1a\x01")
    mocker.patch(
        "jailbee.config._kitty_terminfo_candidates",
        return_value=[f],
    )
    cfg_path = _make_config(tmp_path, "terminal:\n  kitty:\n    enabled: auto\n")
    cfg = load_config(cfg_path)
    m = _kitty_mount(cfg.effective_host_mounts())
    assert m is not None
    assert m.host == f


def test_kitty_auto_mount_suppressed_by_user_supplied_mount(tmp_path, mocker):
    """A user-supplied host_mounts entry whose container path equals the
    kitty target wins over the auto-mount, mirroring chrome/jetbrains."""
    mocker.patch("jailbee.config.detect_default_branch", return_value="main")
    auto = tmp_path / "auto-xterm-kitty"
    auto.write_bytes(b"\x1a\x01")
    user = tmp_path / "user-xterm-kitty"
    user.write_bytes(b"\x1a\x01")
    cfg_path = _make_config(
        tmp_path,
        (
            "terminal:\n  kitty:\n    enabled: true\n"
            f"    host_terminfo_path: {auto}\n"
            "host_mounts:\n"
            f"  - host: {user}\n"
            "    container: /usr/share/terminfo/x/xterm-kitty\n"
            "    readonly: true\n"
        ),
    )
    cfg = load_config(cfg_path)
    m = _kitty_mount(cfg.effective_host_mounts())
    assert m is not None
    assert m.host == user  # user entry wins; auto-add is skipped


# ---------- terminal.kitty validation ----------


def test_kitty_validate_errors_when_enabled_true_and_no_path(tmp_path, mocker):
    mocker.patch("jailbee.config.detect_default_branch", return_value="main")
    mocker.patch(
        "jailbee.config._kitty_terminfo_candidates",
        return_value=[tmp_path / "absent"],
    )
    cfg_path = _make_config(tmp_path, "terminal:\n  kitty:\n    enabled: true\n")
    cfg = load_config(cfg_path)
    issues = cfg.validate_runtime()
    assert any("terminal.kitty" in i and "no kitty terminfo" in i for i in issues)


def test_kitty_validate_errors_when_explicit_path_missing(tmp_path, mocker):
    """A user-supplied path that doesn't exist is always an error, even
    with enabled: auto — the user asked for a specific file."""
    mocker.patch("jailbee.config.detect_default_branch", return_value="main")
    missing = tmp_path / "missing-xterm-kitty"
    cfg_path = _make_config(
        tmp_path,
        f"terminal:\n  kitty:\n    enabled: auto\n    host_terminfo_path: {missing}\n",
    )
    cfg = load_config(cfg_path)
    issues = cfg.validate_runtime()
    assert any("terminal.kitty.host_terminfo_path does not exist" in i for i in issues)


def test_kitty_validate_silent_when_auto_and_no_path(tmp_path, mocker):
    """enabled: auto with no path is a silent no-op — not a validation
    error. The integration just doesn't activate."""
    mocker.patch("jailbee.config.detect_default_branch", return_value="main")
    mocker.patch(
        "jailbee.config._kitty_terminfo_candidates",
        return_value=[tmp_path / "absent"],
    )
    cfg_path = _make_config(tmp_path, "terminal:\n  kitty:\n    enabled: auto\n")
    cfg = load_config(cfg_path)
    issues = cfg.validate_runtime()
    assert not any("terminal.kitty" in i for i in issues)


def test_kitty_validate_silent_when_disabled_and_path_missing(tmp_path, mocker):
    mocker.patch("jailbee.config.detect_default_branch", return_value="main")
    missing = tmp_path / "missing-xterm-kitty"
    cfg_path = _make_config(
        tmp_path,
        f"terminal:\n  kitty:\n    enabled: false\n    host_terminfo_path: {missing}\n",
    )
    cfg = load_config(cfg_path)
    issues = cfg.validate_runtime()
    assert not any("terminal.kitty" in i for i in issues)


# ---------- GithubConfig and GITHUB_API_HOSTS


def test_github_config_defaults_are_disabled():
    from jailbee.config import GithubConfig

    cfg = GithubConfig()
    assert cfg.enabled is False
    assert cfg.api_tokens == {}


def test_github_config_accepts_token_map():
    from jailbee.config import GithubConfig

    cfg = GithubConfig(
        enabled=True,
        api_tokens={"sampleapp": "github_pat_xxx", "personal": "github_pat_yyy"},
    )
    assert cfg.enabled is True
    assert cfg.api_tokens["sampleapp"].get_secret_value() == "github_pat_xxx"
    assert cfg.api_tokens["personal"].get_secret_value() == "github_pat_yyy"


def test_github_config_secretstr_masks_repr():
    from jailbee.config import GithubConfig

    cfg = GithubConfig(enabled=True, api_tokens={"x": "github_pat_secret"})
    assert "github_pat_secret" not in repr(cfg)
    assert "github_pat_secret" not in str(cfg)


def test_github_config_rejects_unknown_fields():
    from pydantic import ValidationError

    from jailbee.config import GithubConfig

    with pytest.raises(ValidationError):
        GithubConfig(enabled=True, api_token_file="/tmp/x")


def test_github_api_hosts_constant_minimal():
    from jailbee.config import GITHUB_API_HOSTS

    assert GITHUB_API_HOSTS == ("api.github.com:443",)


def test_validate_runtime_flags_empty_token_for_this_repo(tmp_path):
    from tests.conftest import make_cfg

    cfg = make_cfg(
        tmp_path,
        container_prefix="sampleapp",
        github={"enabled": True, "api_tokens": {"sampleapp": "   "}},
    )
    issues = cfg.validate_runtime()
    assert any("github.api_tokens['sampleapp'] is empty" in i for i in issues)


def test_validate_runtime_accepts_non_empty_token_for_this_repo(tmp_path):
    from tests.conftest import make_cfg

    cfg = make_cfg(
        tmp_path,
        container_prefix="sampleapp",
        github={"enabled": True, "api_tokens": {"sampleapp": "github_pat_xxx"}},
    )
    issues = cfg.validate_runtime()
    assert not any("github" in i for i in issues)


def test_validate_runtime_silent_when_prefix_absent_from_token_map(tmp_path):
    from tests.conftest import make_cfg

    cfg = make_cfg(
        tmp_path,
        container_prefix="some-other-repo",
        github={"enabled": True, "api_tokens": {"sampleapp": "github_pat_xxx"}},
    )
    issues = cfg.validate_runtime()
    assert not any("github" in i for i in issues)


def test_validate_runtime_silent_when_github_disabled(tmp_path):
    from tests.conftest import make_cfg

    cfg = make_cfg(
        tmp_path,
        container_prefix="sampleapp",
        github={"enabled": False, "api_tokens": {"sampleapp": "   "}},
    )
    issues = cfg.validate_runtime()
    assert not any("github" in i for i in issues)


# --- LooseAutoRevert ---------------------------------------------------------


def test_loose_auto_revert_default_5m():
    """Default values without YAML keys."""
    from datetime import timedelta

    from jailbee.config import LooseAutoRevert

    m = LooseAutoRevert()
    assert m.enabled is True
    assert m.after == "5m"
    assert m.duration() == timedelta(minutes=5)


@pytest.mark.parametrize(
    "value,expected_seconds",
    [
        ("30s", 30),
        ("5m", 300),
        ("2h", 7200),
        (3, 180),  # raw int = minutes
    ],
)
def test_loose_auto_revert_parses_durations(value, expected_seconds):
    from datetime import timedelta

    from jailbee.config import LooseAutoRevert

    m = LooseAutoRevert(after=value)
    assert m.duration() == timedelta(seconds=expected_seconds)


@pytest.mark.parametrize(
    "bad",
    ["five minutes", "", "5", "-3m", 0, -1],
)
def test_loose_auto_revert_rejects_invalid(bad):
    from pydantic import ValidationError

    from jailbee.config import LooseAutoRevert

    with pytest.raises((ValueError, ValidationError)):
        LooseAutoRevert(after=bad).duration()


def test_loose_auto_revert_rejects_over_24h():
    from jailbee.config import LooseAutoRevert

    with pytest.raises(ValueError, match="<= 24h"):
        LooseAutoRevert(after="25h").duration()


def test_loose_auto_revert_rejects_1y_unit():
    """`1y` uses a non-supported unit — duration() should reject it."""
    from jailbee.config import LooseAutoRevert

    with pytest.raises(ValueError, match="invalid duration"):
        LooseAutoRevert(after="1y").duration()


def test_parse_loose_ttl_accepts_the_documented_syntax():
    """The one definition of `--for` / prompt / Qt-dialog duration syntax."""
    from datetime import timedelta

    from jailbee.config import parse_loose_ttl

    assert parse_loose_ttl("30s") == timedelta(seconds=30)
    assert parse_loose_ttl("90m") == timedelta(minutes=90)
    assert parse_loose_ttl("4h") == timedelta(hours=4)
    assert parse_loose_ttl(" 2h ") == timedelta(hours=2)


def test_parse_loose_ttl_never_means_no_auto_revert():
    from jailbee.config import parse_loose_ttl

    assert parse_loose_ttl("never") is None
    assert parse_loose_ttl("NEVER") is None


@pytest.mark.parametrize("bad", ["banana", "2 hours", "2hr", "25h", "0m", "-5m", ""])
def test_parse_loose_ttl_rejects_bad_input(bad):
    from jailbee.config import parse_loose_ttl

    with pytest.raises((ValueError, ValidationError)):
        parse_loose_ttl(bad)


def test_format_loose_after_renders_int_minutes_as_a_duration():
    from jailbee.config import format_loose_after

    assert format_loose_after(5) == "5m"
    assert format_loose_after("45m") == "45m"


def test_effective_loose_auto_revert_inherits_global(tmp_path):
    """Per-repo without the block uses global as-is."""
    from jailbee.config import LooseAutoRevert
    from jailbee.global_config import GlobalConfig
    from tests.conftest import make_cfg

    cfg = make_cfg(tmp_path)
    gcfg = GlobalConfig(
        loose_auto_revert=LooseAutoRevert(enabled=True, after="10m"),
    )
    result = cfg.effective_loose_auto_revert(gcfg)
    assert result is not None
    assert result.enabled is True
    assert result.after == "10m"


def test_effective_loose_auto_revert_per_repo_overrides_field(tmp_path):
    """Per-repo setting only `after` keeps global `enabled`."""
    from jailbee.config import LooseAutoRevert
    from jailbee.global_config import GlobalConfig
    from tests.conftest import make_cfg

    cfg = make_cfg(tmp_path, loose_auto_revert={"after": "30m"})
    gcfg = GlobalConfig(
        loose_auto_revert=LooseAutoRevert(enabled=True, after="5m"),
    )
    result = cfg.effective_loose_auto_revert(gcfg)
    assert result is not None
    assert result.after == "30m"
    assert result.enabled is True


def test_effective_loose_auto_revert_global_disabled_per_repo_after_stays_disabled(tmp_path):
    """Global enabled=false + per-repo `after` only → still disabled."""
    from jailbee.config import LooseAutoRevert
    from jailbee.global_config import GlobalConfig
    from tests.conftest import make_cfg

    cfg = make_cfg(tmp_path, loose_auto_revert={"after": "30m"})
    gcfg = GlobalConfig(
        loose_auto_revert=LooseAutoRevert(enabled=False, after="5m"),
    )
    assert cfg.effective_loose_auto_revert(gcfg) is None


def test_effective_loose_auto_revert_per_repo_disabled(tmp_path):
    from jailbee.config import LooseAutoRevert
    from jailbee.global_config import GlobalConfig
    from tests.conftest import make_cfg

    cfg = make_cfg(tmp_path, loose_auto_revert={"enabled": False})
    gcfg = GlobalConfig(
        loose_auto_revert=LooseAutoRevert(enabled=True, after="5m"),
    )
    assert cfg.effective_loose_auto_revert(gcfg) is None


def test_jetbrains_autostart_defaults_to_false():
    cfg = Config.model_validate({})
    assert cfg.jetbrains.autostart is False


def test_chrome_autostart_defaults_to_false():
    cfg = Config.model_validate({})
    assert cfg.chrome.autostart is False


# --- PullConfig --------------------------------------------------------------


def test_pull_config_defaults_to_prompt():
    """Both cleanup policies default to 'prompt' — backward compatible."""
    from jailbee.config import PullConfig

    m = PullConfig()
    assert m.destroy_container == "prompt"
    assert m.delete_branch == "prompt"


def test_pull_config_accepts_always_and_never():
    from jailbee.config import PullConfig

    m = PullConfig(destroy_container="always", delete_branch="never")
    assert m.destroy_container == "always"
    assert m.delete_branch == "never"


def test_pull_config_rejects_unknown_value():
    from pydantic import ValidationError

    from jailbee.config import PullConfig

    with pytest.raises(ValidationError):
        PullConfig(destroy_container="sometimes")  # type: ignore[arg-type]


def test_pull_config_rejects_extra_keys():
    from pydantic import ValidationError

    from jailbee.config import PullConfig

    with pytest.raises(ValidationError):
        PullConfig.model_validate({"destroy_container": "always", "extra_key": "nope"})


def test_config_parses_pull_section():
    """The `pull:` YAML section flows into Config.pull."""
    from jailbee.config import Config

    cfg = Config.model_validate({"pull": {"destroy_container": "always", "delete_branch": "never"}})
    assert cfg.pull.destroy_container == "always"
    assert cfg.pull.delete_branch == "never"


def test_config_pull_section_defaults_when_absent():
    from jailbee.config import Config

    cfg = Config()
    assert cfg.pull.destroy_container == "prompt"
    assert cfg.pull.delete_branch == "prompt"


# --- pull migration error ----------------------------------------------------


def test_load_config_rejects_legacy_merge_key_in_repo(tmp_path, monkeypatch):
    """A repo .jailbee/config.yaml with the legacy `merge:` block fails clearly."""
    from jailbee.config import ConfigError, load_config

    cfg_dir = tmp_path / ".jailbee"
    cfg_dir.mkdir()
    cfg_path = cfg_dir / "config.yaml"
    cfg_path.write_text("source_repo:\n  path: .\nmerge:\n  destroy_container: always\n")
    monkeypatch.setattr(
        "jailbee.global_config.default_global_config_path",
        lambda: tmp_path / "global-absent.yaml",
    )

    with pytest.raises(ConfigError) as excinfo:
        load_config(cfg_path)
    msg = str(excinfo.value)
    assert "merge:" in msg
    assert "pull:" in msg
    assert str(cfg_path) in msg


def test_load_config_rejects_legacy_merge_key_in_global(tmp_path, monkeypatch):
    """A ~/.config/jailbee/global.yaml with the legacy `merge:` block fails clearly."""
    from jailbee.config import ConfigError, load_config

    cfg_dir = tmp_path / ".jailbee"
    cfg_dir.mkdir()
    cfg_path = cfg_dir / "config.yaml"
    cfg_path.write_text("source_repo:\n  path: .\n")
    global_path = tmp_path / "global.yaml"
    global_path.write_text("merge:\n  destroy_container: always\n")
    monkeypatch.setattr(
        "jailbee.global_config.default_global_config_path",
        lambda: global_path,
    )

    with pytest.raises(ConfigError) as excinfo:
        load_config(cfg_path)
    msg = str(excinfo.value)
    assert "merge:" in msg
    assert "pull:" in msg
    assert str(global_path) in msg


def test_load_config_rejects_legacy_merge_key_in_both_layers(tmp_path, monkeypatch):
    """When both layers carry the legacy key, both paths are listed."""
    from jailbee.config import ConfigError, load_config

    cfg_dir = tmp_path / ".jailbee"
    cfg_dir.mkdir()
    cfg_path = cfg_dir / "config.yaml"
    cfg_path.write_text("source_repo:\n  path: .\nmerge:\n  destroy_container: always\n")
    global_path = tmp_path / "global.yaml"
    global_path.write_text("merge:\n  delete_branch: never\n")
    monkeypatch.setattr(
        "jailbee.global_config.default_global_config_path",
        lambda: global_path,
    )

    with pytest.raises(ConfigError) as excinfo:
        load_config(cfg_path)
    msg = str(excinfo.value)
    assert str(cfg_path) in msg
    assert str(global_path) in msg


# --- NewConfig ---------------------------------------------------------------


def test_new_config_defaults_to_origin_and_autofetch_true():
    from jailbee.config import NewConfig

    n = NewConfig()
    assert n.clone_from == "origin"
    assert n.autofetch is True


def test_new_config_accepts_local_and_autofetch_false():
    from jailbee.config import NewConfig

    n = NewConfig(clone_from="local", autofetch=False)
    assert n.clone_from == "local"
    assert n.autofetch is False


def test_new_config_rejects_unknown_clone_from():
    from pydantic import ValidationError

    from jailbee.config import NewConfig

    with pytest.raises(ValidationError):
        NewConfig(clone_from="remote")  # type: ignore[arg-type]


def test_new_config_rejects_extra_keys():
    from pydantic import ValidationError

    from jailbee.config import NewConfig

    with pytest.raises(ValidationError):
        NewConfig.model_validate({"clone_from": "origin", "extra_key": "nope"})


def test_config_parses_new_section():
    from jailbee.config import Config

    cfg = Config.model_validate({"new": {"clone_from": "local", "autofetch": False}})
    assert cfg.new.clone_from == "local"
    assert cfg.new.autofetch is False


def test_config_new_section_defaults_when_absent():
    from jailbee.config import Config

    cfg = Config()
    assert cfg.new.clone_from == "origin"
    assert cfg.new.autofetch is True


# ---------- PushConfig tests


def test_push_config_defaults():
    """Out-of-the-box defaults: action='ask', source='base'."""
    from jailbee.config import PushConfig

    cfg = PushConfig()
    assert cfg.default_action == "ask"
    assert cfg.default_source == "base"


def test_push_config_ref_defaults():
    """`gie push` resolves the source branch from origin, fetching first.

    Mirrors `new.clone_from='origin'` + `new.autofetch=True`: a host
    `refs/heads/<base>` is only as fresh as the last `git pull`, so the
    remote-tracking copy is the default source of truth.
    """
    from jailbee.config import PushConfig

    cfg = PushConfig()
    assert cfg.push_from == "origin"
    assert cfg.autofetch is True


def test_push_config_accepts_all_documented_values():
    """Every Literal value in the spec must validate."""
    from jailbee.config import PushConfig

    for action in ("merge", "rebase", "plain", "ask"):
        PushConfig(default_action=action)
    for source in ("default-branch", "current", "base", "ask"):
        PushConfig(default_source=source)
    for push_from in ("local", "origin"):
        PushConfig(push_from=push_from)


def test_push_config_rejects_unknown_push_from():
    from pydantic import ValidationError

    from jailbee.config import PushConfig

    with pytest.raises(ValidationError):
        PushConfig(push_from="upstream")


def test_push_config_rejects_unknown_action():
    """Invalid Literal raises a Pydantic validation error."""
    from pydantic import ValidationError

    from jailbee.config import PushConfig

    with pytest.raises(ValidationError):
        PushConfig(default_action="squash")


def test_push_config_rejects_unknown_source():
    from pydantic import ValidationError

    from jailbee.config import PushConfig

    with pytest.raises(ValidationError):
        PushConfig(default_source="upstream")


def test_push_config_extra_keys_forbidden():
    """Typo like `default_actions` (plural) must error, not be silently dropped."""
    from pydantic import ValidationError

    from jailbee.config import PushConfig

    with pytest.raises(ValidationError):
        PushConfig(default_actions="merge")  # type: ignore[call-arg]


def test_config_has_push_field_with_defaults():
    """Config.push exists and uses PushConfig defaults."""
    from jailbee.config import Config, PushConfig

    cfg = Config()
    assert isinstance(cfg.push, PushConfig)
    assert cfg.push.default_action == "ask"
    assert cfg.push.default_source == "base"


def test_config_loads_push_block_from_yaml():
    """A .jailbee/config.yaml `push:` block round-trips through model_validate."""
    from jailbee.config import Config

    cfg = Config.model_validate({"push": {"default_action": "merge", "default_source": "current"}})
    assert cfg.push.default_action == "merge"
    assert cfg.push.default_source == "current"


def test_global_push_layered_under_repo_override(tmp_path):
    """global.yaml provides defaults; .jailbee/config.yaml overrides one key.

    Uses the existing deep_merge pipeline — exercises the merge, not the
    full load_config path (covered by other tests)."""
    from jailbee.config import deep_merge

    global_raw = {"push": {"default_action": "merge", "default_source": "current"}}
    repo_raw = {"push": {"default_action": "rebase"}}

    merged = deep_merge(global_raw, repo_raw)
    assert merged["push"] == {"default_action": "rebase", "default_source": "current"}


def test_push_default_source_accepts_base():
    from jailbee.config import PushConfig

    assert PushConfig(default_source="base").default_source == "base"


def test_push_default_source_default_is_base():
    from jailbee.config import PushConfig

    assert PushConfig().default_source == "base"


def test_effective_shared_caches_adds_claude_install_when_enabled(tmp_path, mocker):
    """claude.enabled auto-adds claude AND claude-install."""
    mocker.patch("jailbee.config.detect_default_branch", return_value="main")
    repo = _write_repo(tmp_path, name="myrepo", config_yaml="claude:\n  enabled: true\n")
    cfg = load_config(repo / ".jailbee" / "config.yaml")
    by_name = {c.name: c for c in cfg.effective_shared_caches()}
    assert "claude-install" in by_name
    assert by_name["claude-install"].host_subpath == "claude-install"
    assert by_name["claude-install"].container_path == "~/.local/share/claude"
    assert "claude-json" not in by_name


def test_effective_shared_caches_no_claude_install_when_disabled(tmp_path, mocker):
    """claude.enabled=false (the default) → no claude-install mount."""
    mocker.patch("jailbee.config.detect_default_branch", return_value="main")
    repo = _write_repo(tmp_path, name="myrepo")
    cfg = load_config(repo / ".jailbee" / "config.yaml")
    names = {c.name for c in cfg.effective_shared_caches()}
    assert "claude-install" not in names


def test_agent_mounts_and_egress_reach_effective_lists(tmp_path):
    """Non-claude agents' mounts and egress flow through the same
    spec-driven path as claude's, not just a claude-only code path."""
    cfg = make_cfg(tmp_path, agents={"codex": {"enabled": True}})
    assert "codex" in [c.name for c in cfg.effective_shared_caches()]
    assert "api.openai.com:443" in cfg.effective_egress_allow()


def test_validate_agents_revalidates_a_plain_agentconfig_under_claude_key():
    """A caller building `Config` in Python (not from YAML) can pass an
    already-constructed base `AgentConfig` under the `claude` key.
    `_validate_agents` must re-validate it through `ClaudeAgentConfig`,
    because `Config.claude` only ever recognises that subclass and falls
    back to a disabled default otherwise — so leaving the base class in
    `agents["claude"]` would split the config in two views that disagree
    on whether Claude is enabled.

    Not reachable from YAML: the dict branch of `_validate_agents` already
    dispatches on the key name, so this is a latent trap only a Python
    caller passing pre-built model instances can hit.
    """
    from jailbee.config import AgentConfig, ClaudeAgentConfig

    cfg = Config.model_validate({"agents": {"claude": AgentConfig(enabled=True, command="claude")}})

    assert isinstance(cfg.agents["claude"], ClaudeAgentConfig)
    # The actual bug: without re-validation, cfg.claude falls back to a
    # disabled default while agents["claude"].enabled stays True — the two
    # views of the same config disagree.
    assert cfg.claude.enabled == cfg.agents["claude"].enabled


def test_validate_runtime_flags_agent_subpath_colliding_with_builtin(tmp_path):
    """An agent whose `shared[].subpath` names a built-in shared subdir would
    have `device_name()` derive the same Incus device name as the built-in
    mount and quietly point it at the agent's container path instead — e.g.
    `ssh`, which is where `jailbee init` seeds the user's real keys.

    Only reported for an *enabled* agent: a disabled entry attaches nothing.
    """
    from jailbee.constants import SHARED_SUBDIRS

    assert "ssh" in SHARED_SUBDIRS  # the collision this test relies on
    cfg = make_cfg(
        tmp_path,
        agents={
            "codex": {
                "enabled": True,
                "command": "codex",
                "shared": [{"subpath": "ssh", "path": "~/.codex"}],
            }
        },
    )

    issues = cfg.validate_runtime()

    assert [i for i in issues if "collides with" in i and "'ssh'" in i]


def test_validate_runtime_ignores_subpath_collision_for_disabled_agent(tmp_path):
    """Differential partner for the test above: the check is gated on
    `enabled`, so a disabled agent with the same colliding subpath is silent.
    Without this, a check that reported unconditionally would still pass."""
    cfg = make_cfg(
        tmp_path,
        agents={
            "codex": {
                "enabled": False,
                "command": "codex",
                "shared": [{"subpath": "ssh", "path": "~/.codex"}],
            }
        },
    )

    assert not [i for i in cfg.validate_runtime() if "collides with" in i]


def test_agent_shared_mount_rejects_seed_on_a_dir(tmp_path, mocker):
    """`seed` names a file to copy in when the shared file is absent, so it is
    meaningless on `type: dir` — and silently ignoring it would leave the user
    believing a directory gets seeded. Rejected at model-validation time."""
    mocker.patch("jailbee.config.detect_default_branch", return_value="main")

    with pytest.raises(ValidationError, match="seed is only valid for type: file"):
        make_cfg(
            tmp_path,
            agents={
                "codex": {
                    "enabled": True,
                    "command": "codex",
                    "shared": [
                        {
                            "subpath": "codex",
                            "path": "~/.codex",
                            "type": "dir",
                            "seed": "~/.codex.example",
                        }
                    ],
                }
            },
        )


def test_agent_shared_mount_allows_seed_on_a_file(tmp_path):
    """The partner case, so the rule above can't be satisfied by rejecting
    every `seed`. Uses a preset-free agent name so the asserted entry is the
    only one in `shared` (a preset's own `shared` list would be merged in
    ahead of it)."""
    cfg = make_cfg(
        tmp_path,
        agents={
            "mystery": {
                "enabled": True,
                "command": "mystery",
                "shared": [
                    {
                        "subpath": "mystery-config",
                        "path": "~/.mystery/config.toml",
                        "type": "file",
                        "seed": "~/.mystery.example",
                    }
                ],
            }
        },
    )

    assert cfg.agents["mystery"].shared[0].seed == "~/.mystery.example"


def test_validate_runtime_flags_enabled_agent_with_empty_command(tmp_path):
    """`enabled: true` with no `command` is a config error, not just something
    `agent_autostart_steps` skips. An agent name outside the shipped six has
    no preset to supply a command, so this is reachable by typo — and the only
    coverage today is the separate autostart-step guard, which is
    defense-in-depth rather than the primary check.
    """
    cfg = make_cfg(tmp_path, agents={"mystery": {"enabled": True, "command": "   "}})

    issues = cfg.validate_runtime()

    assert [i for i in issues if "agents.mystery.enabled=true requires a non-empty `command`" in i]


def test_validate_runtime_silent_for_disabled_agent_with_empty_command(tmp_path):
    """Differential partner: the check is gated on `enabled`."""
    cfg = make_cfg(tmp_path, agents={"mystery": {"enabled": False, "command": ""}})

    assert not [i for i in cfg.validate_runtime() if "non-empty `command`" in i]


def test_new_config_background_defaults_false() -> None:
    from jailbee.config import NewConfig

    assert NewConfig().background is False
    assert NewConfig(background=True).background is True


def test_share_local_defaults_true(make_cfg, tmp_path):
    cfg = make_cfg(tmp_path)
    assert cfg.share_local is True


def test_share_local_override_false(make_cfg, tmp_path):
    cfg = make_cfg(tmp_path, share_local=False)
    assert cfg.share_local is False


def test_share_local_mount_returns_rw_mount_when_dir_exists(make_cfg, tmp_path):
    (tmp_path / ".local").mkdir()
    cfg = make_cfg(tmp_path)
    mount = cfg.share_local_mount()
    assert mount is not None
    assert mount.host == tmp_path / ".local"
    assert mount.container == f"/home/dev/{tmp_path.name}/.local"
    assert mount.readonly is False


def test_share_local_mount_none_when_dir_missing(make_cfg, tmp_path):
    cfg = make_cfg(tmp_path)  # no .local created
    assert cfg.share_local_mount() is None


def test_share_local_mount_none_when_disabled(make_cfg, tmp_path):
    (tmp_path / ".local").mkdir()
    cfg = make_cfg(tmp_path, share_local=False)
    assert cfg.share_local_mount() is None


def test_new_config_submodules_defaults_true() -> None:
    from jailbee.config import NewConfig

    assert NewConfig().submodules is True
    assert NewConfig(submodules=False).submodules is False


# ---------- DestroyConfig


def test_destroy_background_defaults_to_false(tmp_path, mocker):
    mocker.patch("jailbee.config.detect_default_branch", return_value="main")
    cfg_path = _make_config(tmp_path, "{}\n")

    cfg = load_config(cfg_path)

    assert cfg.destroy.background is False


def test_destroy_background_can_be_enabled(tmp_path, mocker):
    mocker.patch("jailbee.config.detect_default_branch", return_value="main")
    cfg_path = _make_config(tmp_path, "destroy:\n  background: true\n")

    cfg = load_config(cfg_path)

    assert cfg.destroy.background is True


# ---------- BootConfig


def test_boot_background_defaults_to_false(tmp_path, mocker):
    mocker.patch("jailbee.config.detect_default_branch", return_value="main")
    cfg_path = _make_config(tmp_path, "{}\n")

    cfg = load_config(cfg_path)

    assert cfg.boot.background is False


def test_boot_background_can_be_enabled(tmp_path, mocker):
    """One key covers both boot commands: `jailbee start` and
    `jailbee restart` run the same slow autostart afterwards."""
    mocker.patch("jailbee.config.detect_default_branch", return_value="main")
    cfg_path = _make_config(tmp_path, "boot:\n  background: true\n")

    cfg = load_config(cfg_path)

    assert cfg.boot.background is True


def test_boot_config_rejects_unknown_keys():
    from pydantic import ValidationError

    from jailbee.config import BootConfig

    with pytest.raises(ValidationError):
        BootConfig.model_validate({"background": True, "restart": True})


def test_claude_ai_pr_description_defaults_true_and_round_trips():
    from jailbee.config import ClaudeAgentConfig

    assert ClaudeAgentConfig().ai_pr_description is True
    assert ClaudeAgentConfig(ai_pr_description=False).ai_pr_description is False


def test_claude_ai_pr_branch_defaults_true():
    from jailbee.config import ClaudeAgentConfig

    assert ClaudeAgentConfig().ai_pr_branch is True


def test_claude_ai_pr_branch_roundtrips_false():
    from jailbee.config import ClaudeAgentConfig

    cfg = ClaudeAgentConfig(ai_pr_branch=False)
    assert cfg.ai_pr_branch is False
    assert cfg.model_dump()["ai_pr_branch"] is False


# ---------- host_devices (declarative host-device passthrough)


def test_host_device_defaults():
    d = HostDevice(path="/dev/kvm")
    assert d.type == "unix-char"
    assert d.mode == "0666"
    assert d.gid is None and d.uid is None
    assert d.effective_source == "/dev/kvm"


def test_host_device_source_overrides_effective_source():
    d = HostDevice(path="/dev/kvm", source="/dev/kvm-host")
    assert d.effective_source == "/dev/kvm-host"


def test_host_device_rejects_relative_path():
    with pytest.raises(ValidationError):
        HostDevice(path="dev/kvm")


def test_host_device_rejects_relative_source():
    with pytest.raises(ValidationError):
        HostDevice(path="/dev/kvm", source="kvm")


def test_host_device_rejects_non_octal_mode():
    with pytest.raises(ValidationError):
        HostDevice(path="/dev/kvm", mode="999")


def test_host_device_accepts_octal_mode():
    assert HostDevice(path="/dev/kvm", mode="0660").mode == "0660"


def test_host_device_rejects_unknown_type():
    with pytest.raises(ValidationError):
        HostDevice(path="/dev/kvm", type="block")


def test_config_host_devices_defaults_empty():
    cfg = Config.model_validate({})
    assert cfg.host_devices == []


def test_config_host_devices_parsed():
    cfg = Config.model_validate({"host_devices": [{"path": "/dev/kvm", "mode": "0666"}]})
    assert cfg.host_devices[0].path == "/dev/kvm"
    assert cfg.host_devices[0].type == "unix-char"


def test_validate_runtime_reports_missing_host_device(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    cfg = _make_runtime_cfg(tmp_path, repo_path=repo)
    cfg = cfg.model_copy(update={"host_devices": [HostDevice(path="/dev/definitely-absent-xyz")]})
    issues = cfg.validate_runtime()
    assert any("host_devices[0]" in issue and "definitely-absent-xyz" in issue for issue in issues)


def test_validate_runtime_silent_for_present_host_device(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    present = tmp_path / "fake-dev"
    present.write_text("")
    cfg = _make_runtime_cfg(tmp_path, repo_path=repo)
    cfg = cfg.model_copy(update={"host_devices": [HostDevice(path=str(present))]})
    assert cfg.validate_runtime() == []


def test_host_device_group_defaults_none():
    assert HostDevice(path="/dev/kvm").group is None


def test_host_device_accepts_valid_group():
    assert HostDevice(path="/dev/kvm", group="kvm").group == "kvm"


def test_host_device_rejects_group_with_shell_metachars():
    with pytest.raises(ValidationError):
        HostDevice(path="/dev/kvm", group="kvm; rm -rf /")


def test_stacks_defaults_all_off():
    s = Stacks()
    assert s.java is False and s.node is False
    assert s.python is False and s.docker is False and s.ecr is False


def test_stacks_accepts_vendor_version_and_bool():
    s = Stacks(java="openjdk-21", node=22, python=True)
    assert s.java == "openjdk-21"
    assert s.node == 22
    assert s.python is True
    assert Stacks(java="corretto-17").java == "corretto-17"
    assert Stacks(java=True).java is True
    assert Stacks(node=True).node is True


def test_stacks_rejects_bad_java_and_unknown_key():
    with pytest.raises(ValidationError):
        Stacks(java="temurin-21")
    with pytest.raises(ValidationError):
        Stacks(java="openjdk")  # missing version
    with pytest.raises(ValidationError):
        Stacks(node=0)  # must be >= 1
    with pytest.raises(ValidationError):
        Stacks(node=-1)  # negatives rejected too
    with pytest.raises(ValidationError):
        Stacks(rust=True)  # unknown stack


def test_stacks_snippet_names_by_vendor_and_deps():
    assert Stacks(java="corretto-17").snippet_names() == ["20-corretto"]
    assert Stacks(java="openjdk-21").snippet_names() == ["20-openjdk"]
    assert Stacks(java=True).snippet_names() == ["20-openjdk"]
    assert Stacks(node=22).snippet_names() == ["30-nodejs"]
    assert Stacks(python=True).snippet_names() == ["40-python"]
    assert Stacks(ecr=True).snippet_names() == ["80-ecr-helper"]
    # java + docker auto-adds the registry-mirror CA import
    assert Stacks(java="corretto-17", docker=True).snippet_names() == [
        "20-corretto",
        "50-docker",
        "90-registry-mirror-ca",
    ]
    # docker alone does not pull in registry-mirror-ca
    assert Stacks(docker=True).snippet_names() == ["50-docker"]


def test_stacks_java_package():
    assert Stacks().java_package() is None
    assert Stacks(java="openjdk-21").java_package() == "openjdk-21-jdk"
    assert Stacks(java="corretto-17").java_package() == "java-17-amazon-corretto-jdk"
    assert Stacks(java=True).java_package() == "default-jdk"


def test_stacks_node_major():
    assert Stacks().node_major() is None
    assert Stacks(node=22).node_major() == 22
    assert Stacks(node=True).node_major() == 24


def test_stacks_shared_caches_by_language():
    assert Stacks().shared_caches() == []
    java = {c.name for c in Stacks(java="openjdk-21").shared_caches()}
    assert java == {"gradle", "m2"}
    node = {c.name for c in Stacks(node=22).shared_caches()}
    assert node == {"npm", "pnpm-store"}
    assert Stacks(python=True).shared_caches() == []


def test_effective_shared_caches_includes_stack_caches(make_cfg, tmp_path):
    cfg = make_cfg(tmp_path)
    cfg = cfg.model_copy(
        update={"golden": cfg.golden.model_copy(update={"stacks": Stacks(java="corretto-17")})}
    )
    names = {c.name for c in cfg.effective_shared_caches()}
    assert {"ssh", "gradle", "m2"} <= names


def test_user_shared_cache_wins_over_stack_autoadd(make_cfg, tmp_path):
    custom = SharedCache(name="m2", host_subpath="custom/m2", container_path="~/.m2")
    cfg = make_cfg(tmp_path)
    cfg = cfg.model_copy(
        update={
            "shared_caches": [custom],
            "golden": cfg.golden.model_copy(update={"stacks": Stacks(java="corretto-17")}),
        }
    )
    m2 = [c for c in cfg.effective_shared_caches() if c.name == "m2"]
    assert len(m2) == 1
    assert m2[0].host_subpath == "custom/m2"


def test_golden_has_stacks_field_defaulting_off():
    assert Golden().stacks == Stacks()
    g = Golden(stacks={"docker": True})
    assert g.stacks.docker is True


# ---- ConfirmConfig ----


def test_confirm_config_defaults_to_on():
    from jailbee.config import ConfirmConfig

    assert ConfirmConfig().auto_target is True


def test_confirm_config_accepts_false():
    from jailbee.config import ConfirmConfig

    assert ConfirmConfig(auto_target=False).auto_target is False


def test_confirm_config_rejects_unknown_keys():
    import pydantic

    from jailbee.config import ConfirmConfig

    with pytest.raises(pydantic.ValidationError):
        ConfirmConfig.model_validate({"auto_target": True, "extra_key": "nope"})


def test_config_exposes_confirm_block_with_default(make_cfg, tmp_path):
    cfg = make_cfg(tmp_path)

    assert cfg.confirm.auto_target is True


def test_config_reads_confirm_from_yaml(tmp_path):
    """A repo config may switch the confirmation off."""
    import yaml

    from jailbee.config import Config

    raw = yaml.safe_load("confirm:\n  auto_target: false\n")
    cfg = Config.model_validate(raw)

    assert cfg.confirm.auto_target is False


def test_defaults_network_rejects_offline() -> None:
    """`defaults.network: offline` must explain the removal, not just fail the enum."""
    from jailbee.config import Config

    with pytest.raises(ValidationError, match="was removed"):
        Config.model_validate({"defaults": {"network": "offline"}})


def test_autostart_step_network_rejects_offline() -> None:
    from jailbee.config import AutostartStep

    with pytest.raises(ValidationError, match="was removed"):
        AutostartStep.model_validate({"name": "x", "run": "true", "network": "offline"})


def test_autostart_step_network_still_accepts_strict_and_loose() -> None:
    from jailbee.config import AutostartStep

    assert AutostartStep(name="x", run="true", network="strict").network == "strict"
    assert AutostartStep(name="x", run="true", network="loose").network == "loose"
    assert AutostartStep(name="x", run="true").network is None


def test_column_config_defaults_are_empty() -> None:
    from jailbee.config import ColumnConfig

    c = ColumnConfig()
    assert c.fields is None
    assert c.hide == []


def test_column_config_rejects_unknown_keys() -> None:
    import pytest
    from pydantic import ValidationError

    from jailbee.config import ColumnConfig

    with pytest.raises(ValidationError):
        ColumnConfig.model_validate({"feilds": ["name"]})


def test_global_dashboard_hide_defaults_to_todays_hidden_set() -> None:
    """The dashboard must look identical until someone edits config."""
    from jailbee.config import DASHBOARD_DEFAULT_HIDE
    from jailbee.global_config import GlobalConfig

    assert sorted(GlobalConfig().dashboard.hide) == sorted(DASHBOARD_DEFAULT_HIDE)
    assert GlobalConfig().ls.hide == []
    assert GlobalConfig().ls.fields is None


def test_effective_ls_columns_falls_back_to_global(make_cfg, tmp_path) -> None:
    from jailbee.config import ColumnConfig
    from jailbee.global_config import GlobalConfig

    cfg = make_cfg(tmp_path)
    gcfg = GlobalConfig(ls=ColumnConfig(fields=["name", "state"]))

    assert cfg.effective_ls_columns(gcfg).fields == ["name", "state"]


def test_effective_ls_columns_repo_overrides_global(make_cfg, tmp_path) -> None:
    from jailbee.config import ColumnConfig
    from jailbee.global_config import GlobalConfig

    cfg = make_cfg(tmp_path, ls={"fields": ["name", "ip"]})
    gcfg = GlobalConfig(ls=ColumnConfig(fields=["name", "state"]))

    assert cfg.effective_ls_columns(gcfg).fields == ["name", "ip"]


def test_effective_columns_merge_per_field_not_wholesale(make_cfg, tmp_path) -> None:
    """A repo that sets only `hide` must keep the global `fields`."""
    from jailbee.config import ColumnConfig
    from jailbee.global_config import GlobalConfig

    cfg = make_cfg(tmp_path, ls={"hide": ["ip"]})
    gcfg = GlobalConfig(ls=ColumnConfig(fields=["name", "state"]))

    eff = cfg.effective_ls_columns(gcfg)
    assert eff.fields == ["name", "state"]
    assert eff.hide == ["ip"]


def test_effective_columns_explicit_empty_hide_beats_a_nonempty_global(make_cfg, tmp_path) -> None:
    """An explicitly set `hide: []` in the repo block must win over a
    non-empty global `hide`, because the merge is keyed on which fields
    were *set* (`model_fields_set`), not on whether their values are
    truthy. A merge keyed on truthiness would treat an empty list as
    "not set" and let the global value leak through instead."""
    from jailbee.config import ColumnConfig
    from jailbee.global_config import GlobalConfig

    cfg = make_cfg(tmp_path, ls={"hide": []})
    gcfg = GlobalConfig(ls=ColumnConfig(hide=["ip"]))

    eff = cfg.effective_ls_columns(gcfg)
    assert eff.hide == []


def test_validate_runtime_rejects_an_unknown_ls_field(make_cfg, tmp_path) -> None:
    cfg = make_cfg(tmp_path, ls={"fields": ["name", "nosuchfield"]})

    issues = cfg.validate_runtime()

    assert any("nosuchfield" in i for i in issues)
    assert any("allowed:" in i for i in issues)


def test_validate_runtime_rejects_an_unknown_hidden_field(make_cfg, tmp_path) -> None:
    cfg = make_cfg(tmp_path, dashboard={"hide": ["nosuchfield"]})

    assert any("nosuchfield" in i for i in cfg.validate_runtime())


def test_validate_runtime_accepts_real_field_names(make_cfg, tmp_path) -> None:
    cfg = make_cfg(
        tmp_path,
        ls={"fields": ["name", "state", "local_diff", "local_count"]},
        dashboard={"hide": ["created"]},
    )

    assert not [i for i in cfg.validate_runtime() if "unknown field" in i]


def test_validate_runtime_rejects_an_empty_fields_list(make_cfg, tmp_path) -> None:
    """`fields: []` would render a table with no columns at all; `fields: null`
    is how you ask for the built-in default set."""
    cfg = make_cfg(tmp_path, ls={"fields": []})

    issues = cfg.validate_runtime()

    assert any("fields is empty" in i for i in issues)
    assert any("fields: null" in i for i in issues)


def test_validate_runtime_rejects_a_duplicated_field(make_cfg, tmp_path) -> None:
    cfg = make_cfg(tmp_path, dashboard={"fields": ["name", "state", "name"]})

    issues = cfg.validate_runtime()

    assert any("duplicate field 'name'" in i for i in issues)


def test_validate_runtime_accepts_a_repeated_hidden_field(make_cfg, tmp_path) -> None:
    """`hide` is subtractive and idempotent, so a repeat is harmless."""
    cfg = make_cfg(tmp_path, ls={"hide": ["ip", "ip"]})

    assert cfg.validate_runtime() == []


# ---------- sanitize_column_blocks (the recovery-flavoured counterpart of
# validate_column_blocks, used by global_config.load_global_config so a
# typo'd column name in global.yaml never breaks an unrelated command)


def test_sanitize_column_blocks_drops_an_unknown_name() -> None:
    from jailbee.config import ColumnConfig, sanitize_column_blocks

    fixed, warnings = sanitize_column_blocks([("ls", ColumnConfig(fields=["name", "nope"]))])

    assert fixed["ls"].fields == ["name"]
    assert any("nope" in w and "allowed:" in w for w in warnings)


def test_sanitize_column_blocks_drops_an_unknown_hidden_name() -> None:
    from jailbee.config import ColumnConfig, sanitize_column_blocks

    fixed, warnings = sanitize_column_blocks([("ls", ColumnConfig(hide=["ip", "nope"]))])

    assert fixed["ls"].hide == ["ip"]
    assert any("nope" in w for w in warnings)


def test_sanitize_column_blocks_dedupes_keeping_the_first_occurrence() -> None:
    from jailbee.config import ColumnConfig, sanitize_column_blocks

    fixed, warnings = sanitize_column_blocks([("ls", ColumnConfig(fields=["name", "ip", "name"]))])

    assert fixed["ls"].fields == ["name", "ip"]
    assert any("duplicate" in w for w in warnings)


def test_sanitize_column_blocks_resets_an_explicit_empty_list_to_defaults() -> None:
    from jailbee.config import ColumnConfig, sanitize_column_blocks

    fixed, warnings = sanitize_column_blocks([("dashboard", ColumnConfig(fields=[]))])

    assert fixed["dashboard"].fields is None
    assert any("empty" in w for w in warnings)


def test_sanitize_column_blocks_resets_to_defaults_when_every_name_was_bad() -> None:
    """`fields` reduced to nothing by dropping unknown names is the same
    "no columns" problem as an explicit `fields: []`, not a separate error."""
    from jailbee.config import ColumnConfig, sanitize_column_blocks

    fixed, warnings = sanitize_column_blocks([("ls", ColumnConfig(fields=["nope"]))])

    assert fixed["ls"].fields is None
    assert any("no valid column names remained" in w for w in warnings)


def test_sanitize_column_blocks_leaves_a_valid_block_untouched() -> None:
    from jailbee.config import ColumnConfig, sanitize_column_blocks

    fixed, warnings = sanitize_column_blocks([("ls", ColumnConfig(fields=["name", "state"]))])

    assert fixed["ls"].fields == ["name", "state"]
    assert warnings == []


def test_sanitize_column_blocks_leaves_the_default_null_fields_alone() -> None:
    from jailbee.config import ColumnConfig, sanitize_column_blocks

    fixed, warnings = sanitize_column_blocks([("ls", ColumnConfig())])

    assert fixed["ls"].fields is None
    assert warnings == []


# ---------- column blocks through the real load path
#
# The unit tests above hand-build `GlobalConfig(ls=ColumnConfig(...))`. These
# write BOTH YAML files to disk and go through `load_config` /
# `load_global_config`, because the merge layer that actually runs in
# production is not the one a hand-built GlobalConfig exercises: `ls` and
# `dashboard` are host-level keys (`config._HOST_LEVEL_KEYS`), so a global
# block must land in `GlobalConfig` and never in `Config`.


def _load_global_yaml(path):
    """`load_global_config(path)`'s config half, discarding the warnings list.

    Imported lazily like the rest of this file. Warning-focused coverage
    (an unknown/empty/duplicate name in the global blocks) lives in
    `test_global_config.py`, next to `load_global_config` itself; these
    tests are about the merge layer (`effective_ls_columns` et al.), which
    only cares about the resulting `GlobalConfig`.
    """
    from jailbee.global_config import load_global_config

    gcfg, _ = load_global_config(path)
    return gcfg


def _write_layered(tmp_path, monkeypatch, *, global_yaml: str, repo_yaml: str = "{}"):
    """Write ~/.config/jailbee/global.yaml + <repo>/.jailbee/config.yaml, return both."""
    xdg = tmp_path / ".config"
    (xdg / "jailbee").mkdir(parents=True)
    (xdg / "jailbee" / "global.yaml").write_text(global_yaml)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    repo = _write_repo(tmp_path, name="myrepo", config_yaml=repo_yaml)
    return repo / ".jailbee" / "config.yaml", xdg / "jailbee" / "global.yaml"


def test_load_path_global_column_block_goes_to_global_config_only(
    tmp_path, monkeypatch, mocker
) -> None:
    """A global `ls:` block must reach `GlobalConfig.ls` — it used to be
    discarded by `_split_host_keys` and silently merged into `Config.ls`
    instead, so `effective_ls_columns`' `gcfg` argument was dead."""
    mocker.patch("jailbee.config.detect_default_branch", return_value="main")
    cfg_path, global_path = _write_layered(
        tmp_path,
        monkeypatch,
        global_yaml="ls:\n  fields: [name, state]\n  hide: [ip]\n",
    )

    cfg = load_config(cfg_path)
    gcfg = _load_global_yaml(global_path)

    assert gcfg.ls.fields == ["name", "state"]
    assert gcfg.ls.hide == ["ip"]
    # Nothing leaked into the Config layer, so the repo block stays default.
    assert cfg.ls.fields is None
    assert cfg.ls.hide == []
    assert cfg.effective_ls_columns(gcfg).fields == ["name", "state"]


def test_load_path_repo_block_overrides_global_without_appending(
    tmp_path, monkeypatch, mocker
) -> None:
    """The regression guard for the whole finding: `deep_merge`'s list rule
    *appends* a non-empty overlay, so routing these blocks through it turned
    global `[name, state]` + repo `[name, ip]` into
    `[name, state, name, ip]` — NAME rendered twice."""
    mocker.patch("jailbee.config.detect_default_branch", return_value="main")
    cfg_path, global_path = _write_layered(
        tmp_path,
        monkeypatch,
        global_yaml="ls:\n  fields: [name, state]\n  hide: [ip]\n",
        repo_yaml="ls:\n  fields: [name, ip]\n  hide: [mem]\n",
    )

    cfg = load_config(cfg_path)
    columns = cfg.effective_ls_columns(_load_global_yaml(global_path))

    assert columns.fields == ["name", "ip"]
    assert columns.hide == ["mem"]


def test_load_path_repo_hide_only_inherits_the_global_fields(tmp_path, monkeypatch, mocker) -> None:
    """The documented per-field override, asserted through the real loaders."""
    mocker.patch("jailbee.config.detect_default_branch", return_value="main")
    cfg_path, global_path = _write_layered(
        tmp_path,
        monkeypatch,
        global_yaml="ls:\n  fields: [name, state]\n",
        repo_yaml="ls:\n  hide: [ip]\n",
    )

    columns = load_config(cfg_path).effective_ls_columns(_load_global_yaml(global_path))

    assert columns.fields == ["name", "state"]
    assert columns.hide == ["ip"]


def test_load_path_repo_explicit_empty_hide_beats_a_nonempty_global_ls(
    tmp_path, monkeypatch, mocker
) -> None:
    """End-to-end guard for the unit-level property proved by
    `test_effective_columns_explicit_empty_hide_beats_a_nonempty_global`: an
    explicit `hide: []` in the repo's real `.jailbee/config.yaml` must still win
    over a non-empty global `hide` once both files have gone through
    `load_config`/`load_global_config` and their sanitizers, not just
    through a hand-built `Config`/`GlobalConfig` pair."""
    mocker.patch("jailbee.config.detect_default_branch", return_value="main")
    cfg_path, global_path = _write_layered(
        tmp_path,
        monkeypatch,
        global_yaml="ls:\n  hide: [ip]\n",
        repo_yaml="ls:\n  hide: []\n",
    )

    columns = load_config(cfg_path).effective_ls_columns(_load_global_yaml(global_path))

    assert columns.hide == []


def test_load_path_repo_null_fields_resets_a_global_fields_list(
    tmp_path, monkeypatch, mocker
) -> None:
    """`fields: null` in the repo is how you undo a global list (an explicit
    `fields: []` recovers to the built-in default set rather than "no
    columns at all" — see `test_global_config.py`)."""
    mocker.patch("jailbee.config.detect_default_branch", return_value="main")
    cfg_path, global_path = _write_layered(
        tmp_path,
        monkeypatch,
        global_yaml="ls:\n  fields: [name, state]\n",
        repo_yaml="ls:\n  fields: null\n",
    )

    columns = load_config(cfg_path).effective_ls_columns(_load_global_yaml(global_path))

    assert columns.fields is None


def test_load_path_global_dashboard_block_reaches_global_config(
    tmp_path, monkeypatch, mocker
) -> None:
    mocker.patch("jailbee.config.detect_default_branch", return_value="main")
    _, global_path = _write_layered(
        tmp_path,
        monkeypatch,
        global_yaml="dashboard:\n  hide: [ip]\n",
    )

    gcfg = _load_global_yaml(global_path)

    # `hide` *replaces* DASHBOARD_DEFAULT_HIDE rather than extending it.
    assert gcfg.dashboard.hide == ["ip"]


def test_load_path_agents_layer_merges_per_agent_instead_of_replacing(
    tmp_path, monkeypatch, mocker
) -> None:
    """The mapping-not-list decision, asserted at the layer that motivated it.

    `agents:` is a mapping precisely so a repo can extend an entry the user
    enabled globally. If it were a list, `deep_merge`'s list rule would append
    and the codex entry would appear twice; if the mapping merge were shallow,
    the repo's `egress_allow` would replace the whole entry and lose
    `enabled: true`. Neither is covered anywhere else — every other
    `agents:` test builds one dict.
    """
    mocker.patch("jailbee.config.detect_default_branch", return_value="main")
    cfg_path, _ = _write_layered(
        tmp_path,
        monkeypatch,
        global_yaml="agents:\n  codex:\n    enabled: true\n",
        repo_yaml="agents:\n  codex:\n    egress_allow: [extra.host:443]\n",
    )

    cfg = load_config(cfg_path)

    # One entry, not two, and the globally-set scalar survived the repo layer.
    assert list(cfg.agents) == ["codex"]
    assert cfg.agents["codex"].enabled is True
    # The preset's own host is still there (preset merges under both layers),
    # and the repo's addition appended rather than replacing.
    assert cfg.agents["codex"].egress_allow == ["api.openai.com:443", "extra.host:443"]
    # The preset's other fields survived too — the repo entry was a fragment.
    assert cfg.agents["codex"].command == "codex"
    assert "extra.host:443" in cfg.effective_egress_allow()


def test_load_path_legacy_global_claude_plus_repo_agents_claude_is_an_error(
    tmp_path, monkeypatch, mocker
) -> None:
    """The exact shape an existing user hits: a `claude:` block left in
    `global.yaml` (what the old template wrote) meeting an `agents.claude`
    in a repo config. This is a *new hard failure* for such users, and the
    only direct coverage today is `resolve_agents_raw` with both keys in one
    dict — which never exercises the cross-layer path or the error text.

    The message must name both files, so it points at `global.yaml` (which
    holds the key to delete) rather than only at the repo config the
    surrounding `Config validation failed in ...` wrapper would name.
    """
    mocker.patch("jailbee.config.detect_default_branch", return_value="main")
    cfg_path, global_path = _write_layered(
        tmp_path,
        monkeypatch,
        global_yaml="claude:\n  enabled: true\n",
        repo_yaml="agents:\n  claude:\n    autostart: true\n",
    )

    with pytest.raises(ConfigError) as exc_info:
        load_config(cfg_path)

    message = str(exc_info.value)
    assert "claude:" in message and "agents.claude" in message
    assert str(global_path) in message
    assert str(cfg_path) in message


def test_load_path_legacy_claude_alone_still_loads(tmp_path, monkeypatch, mocker) -> None:
    """The guard above must not fire on the legacy spelling by itself —
    a `claude:` block in `global.yaml` with no `agents.claude` anywhere is
    still a supported config."""
    mocker.patch("jailbee.config.detect_default_branch", return_value="main")
    cfg_path, _ = _write_layered(
        tmp_path,
        monkeypatch,
        global_yaml="claude:\n  enabled: true\n",
        repo_yaml="agents:\n  codex:\n    enabled: true\n",
    )

    cfg = load_config(cfg_path)

    assert cfg.claude.enabled is True
    assert cfg.agents["codex"].enabled is True


# A typo, an empty `fields: []`, or a duplicated name in `global.yaml`'s
# column blocks no longer raises out of `load_global_config` — a personal
# display preference must not break an unrelated command. Coverage for that
# recovery (and for `gie config validate` still failing on the same typo)
# lives in `test_global_config.py`, next to `load_global_config` and
# `global_config_issues` themselves.


def test_load_global_config_accepts_valid_column_blocks(tmp_path) -> None:
    path = tmp_path / "global.yaml"
    path.write_text("ls:\n  fields: [name, local_diff]\ndashboard:\n  hide: [ip]\n")

    gcfg = _load_global_yaml(path)

    assert gcfg.ls.fields == ["name", "local_diff"]


# ---------- repo-layer column blocks are sanitized too (`load_config`,
# not just `load_global_config`): a typo'd column name in a repo's
# `.jailbee/config.yaml` must not render a zero-column table for everyone
# working in that repo, same as `global.yaml` already recovers from one.
# `gie config validate` (via `load_config_unsanitized`) still fails on all
# three — covered in test_cli.py, next to the equivalent global-layer test.


def test_load_config_resets_an_explicit_empty_repo_fields_to_defaults(tmp_path, mocker) -> None:
    mocker.patch("jailbee.config.detect_default_branch", return_value="main")
    repo = _write_repo(tmp_path, config_yaml="ls:\n  fields: []\n")

    cfg = load_config(repo / ".jailbee" / "config.yaml")

    assert cfg.ls.fields is None
    assert any("empty" in w for w in cfg.column_warnings())


def test_load_config_drops_unknown_and_duplicate_repo_field_names(tmp_path, mocker) -> None:
    mocker.patch("jailbee.config.detect_default_branch", return_value="main")
    repo = _write_repo(tmp_path, config_yaml="ls:\n  fields: [name, nosuchfield, name]\n")

    cfg = load_config(repo / ".jailbee" / "config.yaml")

    assert cfg.ls.fields == ["name"]
    warnings = cfg.column_warnings()
    assert any("nosuchfield" in w and "allowed:" in w for w in warnings)
    assert any("duplicate" in w for w in warnings)


def test_load_config_drops_an_unknown_repo_hide_name(tmp_path, mocker) -> None:
    mocker.patch("jailbee.config.detect_default_branch", return_value="main")
    repo = _write_repo(tmp_path, config_yaml="dashboard:\n  hide: [ip, nosuchfield]\n")

    cfg = load_config(repo / ".jailbee" / "config.yaml")

    assert cfg.dashboard.hide == ["ip"]
    assert any("nosuchfield" in w for w in cfg.column_warnings())


def test_load_config_leaves_a_valid_repo_column_block_untouched(tmp_path, mocker) -> None:
    mocker.patch("jailbee.config.detect_default_branch", return_value="main")
    repo = _write_repo(tmp_path, config_yaml="ls:\n  fields: [name, state]\n")

    cfg = load_config(repo / ".jailbee" / "config.yaml")

    assert cfg.ls.fields == ["name", "state"]
    assert cfg.column_warnings() == []


def test_load_config_column_warnings_empty_by_default(tmp_path, mocker) -> None:
    mocker.patch("jailbee.config.detect_default_branch", return_value="main")
    repo = _write_repo(tmp_path, config_yaml="{}\n")

    cfg = load_config(repo / ".jailbee" / "config.yaml")

    assert cfg.column_warnings() == []


def test_load_config_repo_ls_hide_only_still_inherits_global_fields_after_sanitize(
    tmp_path, monkeypatch, mocker
) -> None:
    """`sanitize_column_blocks` must only touch the sub-field it is actually
    correcting: a repo `ls:` block that sets `hide` (with one bad name to
    sanitize) but never mentions `fields` must still inherit the global
    `fields` through `effective_ls_columns` after going through
    `load_config` — if the sanitizer reconstructed the whole `ColumnConfig`
    instead of touching only `hide`, it would mark `fields` as explicitly
    set too (to its own default, `None`), which would override the global
    `fields` list with `None` instead of inheriting it, corrupting the merge
    for every repo that ever has a `hide` typo, not just the sanitizer's own
    unit tests. `ls` is the live path this guards; the equivalent for the
    deprecated `dashboard:` block was deleted along with
    `effective_dashboard_columns`."""
    mocker.patch("jailbee.config.detect_default_branch", return_value="main")
    cfg_path, global_path = _write_layered(
        tmp_path,
        monkeypatch,
        global_yaml="ls:\n  fields: [name, state]\n",
        repo_yaml="ls:\n  hide: [ip, nosuchfield]\n",
    )

    cfg = load_config(cfg_path)
    gcfg = _load_global_yaml(global_path)

    eff = cfg.effective_ls_columns(gcfg)
    assert eff.fields == ["name", "state"]
    assert eff.hide == ["ip"]


# ---------- load_config_from_text seam


def test_load_config_from_text_matches_file_load(tmp_path, mocker):
    """Identical YAML must produce an identical Config via text and via file."""
    from jailbee.config import load_config_from_text, load_config_unsanitized

    mocker.patch(
        "jailbee.global_config.default_global_config_path",
        return_value=tmp_path / "no-such-global.yaml",
    )
    text = "autostart:\n  on_create:\n    - name: build\n      run: make\n"
    # Nested under "myrepo" (not tmp_path directly) so the derived
    # container_prefix — repo_root.name — matches _PREFIX_RE; pytest's
    # tmp_path directory name contains underscores from the test id.
    gie_dir = tmp_path / "myrepo" / ".gie"
    gie_dir.mkdir(parents=True)
    cfg_path = gie_dir / "config.yaml"
    cfg_path.write_text(text)

    from_file = load_config_unsanitized(cfg_path)
    from_text = load_config_from_text(text, cfg_path)

    assert from_text.autostart == from_file.autostart
    assert from_text.repo_root == from_file.repo_root
    assert from_text.container_prefix == from_file.container_prefix


def test_load_config_from_text_does_not_read_the_path(tmp_path, mocker):
    """The path is for repo_root derivation only — the file need not exist."""
    from jailbee.config import load_config_from_text

    mocker.patch(
        "jailbee.global_config.default_global_config_path",
        return_value=tmp_path / "no-such-global.yaml",
    )
    # Nested under "myrepo" so the derived container_prefix (repo_root.name)
    # matches _PREFIX_RE; pytest's tmp_path name contains underscores.
    cfg_path = tmp_path / "myrepo" / ".gie" / "config.yaml"  # deliberately never created

    cfg = load_config_from_text("autostart:\n  on_start: []\n", cfg_path)

    assert cfg.autostart.on_start == []
    assert not cfg_path.exists()


def test_load_config_from_text_preserves_global_list_append(tmp_path, mocker):
    """deep_merge appends lists — the seam must not bypass the global layer."""
    from jailbee.config import load_config_from_text

    global_path = tmp_path / "global.yaml"
    global_path.write_text("autostart:\n  on_create:\n    - name: from-global\n      run: 'true'\n")
    mocker.patch(
        "jailbee.global_config.default_global_config_path",
        return_value=global_path,
    )
    # Nested under "myrepo" so the derived container_prefix (repo_root.name)
    # matches _PREFIX_RE; pytest's tmp_path name contains underscores.
    cfg_path = tmp_path / "myrepo" / ".gie" / "config.yaml"

    cfg = load_config_from_text(
        "autostart:\n  on_create:\n    - name: from-repo\n      run: make\n", cfg_path
    )

    assert [s.name for s in cfg.autostart.on_create] == ["from-global", "from-repo"]


def test_load_config_from_text_raises_on_invalid_yaml(tmp_path, mocker):
    from jailbee.config import ConfigError, load_config_from_text

    mocker.patch(
        "jailbee.global_config.default_global_config_path",
        return_value=tmp_path / "no-such-global.yaml",
    )
    with pytest.raises(ConfigError):
        load_config_from_text("autostart: [unclosed", tmp_path / ".jailbee" / "config.yaml")


def test_load_config_from_text_rejects_github_block(tmp_path, mocker):
    """The placement ban must apply to text-loaded configs too."""
    from jailbee.config import ConfigError, load_config_from_text

    mocker.patch(
        "jailbee.global_config.default_global_config_path",
        return_value=tmp_path / "no-such-global.yaml",
    )
    with pytest.raises(ConfigError, match="github"):
        load_config_from_text("github:\n  enabled: true\n", tmp_path / ".jailbee" / "config.yaml")


def test_host_ports_defaults(tmp_path):
    cfg_path = _make_config(
        tmp_path,
        "host_ports:\n  - name: adb\n    port: 5037\n",
    )
    cfg = load_config(cfg_path)
    entry = cfg.host_ports[0]
    assert entry.name == "adb"
    assert entry.port == 5037
    assert entry.host_port is None
    assert entry.effective_host_port == 5037
    assert entry.proto == "tcp"
    assert entry.host_address == "127.0.0.1"
    assert entry.container_address == "127.0.0.1"


def test_host_ports_explicit_host_port_and_udp(tmp_path):
    cfg_path = _make_config(
        tmp_path,
        "host_ports:\n"
        "  - name: dns\n"
        "    port: 5353\n"
        "    host_port: 53\n"
        "    proto: udp\n"
        "    host_address: 10.0.0.1\n"
        "    container_address: 127.0.0.2\n",
    )
    entry = load_config(cfg_path).host_ports[0]
    assert entry.effective_host_port == 53
    assert entry.proto == "udp"
    assert entry.host_address == "10.0.0.1"
    assert entry.container_address == "127.0.0.2"


def test_host_ports_default_is_empty(tmp_path):
    assert load_config(_make_config(tmp_path, "{}\n")).host_ports == []


@pytest.mark.parametrize(
    "name",
    ["Adb", "-adb", "adb_server", "adb server", "a" * 41],
)
def test_host_ports_rejects_bad_name(tmp_path, name):
    cfg_path = _make_config(tmp_path, f"host_ports:\n  - name: {name!r}\n    port: 5037\n")
    with pytest.raises(ConfigError, match="host_ports name"):
        load_config(cfg_path)


@pytest.mark.parametrize("port", [0, 70000, -1])
def test_host_ports_rejects_out_of_range_port(tmp_path, port):
    cfg_path = _make_config(tmp_path, f"host_ports:\n  - name: x\n    port: {port}\n")
    with pytest.raises(ConfigError, match=re.escape("1..65535")):
        load_config(cfg_path)


def test_host_ports_rejects_out_of_range_host_port(tmp_path):
    cfg_path = _make_config(
        tmp_path,
        "host_ports:\n  - name: x\n    port: 5037\n    host_port: 99999\n",
    )
    with pytest.raises(ConfigError, match=re.escape("1..65535")):
        load_config(cfg_path)


@pytest.mark.parametrize("field", ["host_address", "container_address"])
def test_host_ports_rejects_hostname_as_address(tmp_path, field):
    cfg_path = _make_config(
        tmp_path,
        f"host_ports:\n  - name: x\n    port: 5037\n    {field}: localhost\n",
    )
    with pytest.raises(ConfigError, match="IP literal"):
        load_config(cfg_path)


def test_host_ports_accepts_ipv6_literal(tmp_path):
    cfg_path = _make_config(
        tmp_path,
        'host_ports:\n  - name: x\n    port: 5037\n    host_address: "::1"\n',
    )
    assert load_config(cfg_path).host_ports[0].host_address == "::1"


def test_host_ports_rejects_duplicate_names(tmp_path):
    cfg_path = _make_config(
        tmp_path,
        "host_ports:\n  - name: adb\n    port: 5037\n  - name: adb\n    port: 5038\n",
    )
    with pytest.raises(ConfigError, match="duplicate host_ports name"):
        load_config(cfg_path)


@pytest.mark.parametrize("key", ["direction", "to_host", "bind"])
def test_host_ports_rejects_direction_keys_with_explanation(tmp_path, key):
    cfg_path = _make_config(
        tmp_path,
        f"host_ports:\n  - name: web\n    port: 8080\n    {key}: to_host\n",
    )
    with pytest.raises(ConfigError, match="jailbee port to-host"):
        load_config(cfg_path)


def test_host_ports_rejects_unknown_key(tmp_path):
    cfg_path = _make_config(
        tmp_path,
        "host_ports:\n  - name: web\n    port: 8080\n    nat: true\n",
    )
    with pytest.raises(ConfigError):
        load_config(cfg_path)


def test_a_global_dashboard_block_is_reported_as_deprecated(tmp_path) -> None:
    """`jailbee config validate` is where a user finds out the block stopped
    mattering. Only fires when the key is actually present — a default block
    must stay silent."""
    from jailbee.global_config import global_config_issues

    path = tmp_path / "global.yaml"
    path.write_text("dashboard:\n  hide: [mem]\n")

    issues = global_config_issues(path)

    assert any("dashboard" in i and "deprecated" in i for i in issues)


def test_no_dashboard_block_reports_no_deprecation(tmp_path) -> None:
    from jailbee.global_config import global_config_issues

    path = tmp_path / "global.yaml"
    path.write_text("ls:\n  hide: [mem]\n")

    assert not any("deprecated" in i for i in global_config_issues(path))


def test_a_repo_dashboard_block_reports_deprecated_and_not_seeded(make_cfg, tmp_path) -> None:
    """A repo block is doubly dead: deprecated like the global one, and never
    seeded — the seed reads the personal layer only, so this one is simply
    dropped. That has to be said out loud, not left to be discovered."""
    from jailbee.config import ColumnConfig

    # `model_copy(update=...)` adds the updated keys to __pydantic_fields_set__
    # in Pydantic v2, which is what `"dashboard" in cfg.model_fields_set` reads.
    cfg = make_cfg(tmp_path).model_copy(update={"dashboard": ColumnConfig(hide=["mem"])})
    assert "dashboard" in cfg.model_fields_set  # the precondition this test rests on

    issues = cfg.validate_runtime()

    assert any("deprecated" in i for i in issues)
    assert any("not seeded" in i or "not imported" in i for i in issues)


def test_claude_credentials_dir_for_uses_the_default_group():
    from jailbee.config import ClaudeCredentials
    from jailbee.paths import xdg_data_home

    creds = ClaudeCredentials(group="work")

    assert creds.dir_for("anything") == xdg_data_home() / "jailbee" / "claude-credentials" / "work"


def test_claude_credentials_dir_for_prefers_a_repo_entry():
    from jailbee.config import ClaudeCredentials
    from jailbee.paths import xdg_data_home

    creds = ClaudeCredentials(group="work", repos={"side": "personal"})

    root = xdg_data_home() / "jailbee" / "claude-credentials"
    assert creds.dir_for("side") == root / "personal"
    assert creds.dir_for("other") == root / "work"


def test_claude_credentials_explicit_null_opts_a_repo_out():
    """An explicit `null` must beat the default group, not fall through to it —
    it is the only way to keep one repo on its own credential."""
    from jailbee.config import ClaudeCredentials

    creds = ClaudeCredentials(group="work", repos={"solo": None})

    assert creds.dir_for("solo") is None
    assert creds.dir_for("other") is not None


def test_claude_credentials_absent_block_shares_nothing():
    from jailbee.config import ClaudeCredentials

    assert ClaudeCredentials().dir_for("anything") is None


def test_claude_credentials_rejects_a_group_name_that_is_not_one_path_segment():
    """The group name becomes a directory name under the credentials root, so
    a traversal must be refused at validation, not sanitised later."""
    import pytest
    from pydantic import ValidationError

    from jailbee.config import ClaudeCredentials

    for bad in ("../escape", "with/slash", "Upper", "-leading", ""):
        with pytest.raises(ValidationError):
            ClaudeCredentials(group=bad)
    with pytest.raises(ValidationError):
        ClaudeCredentials(repos={"repo": "../escape"})


def test_gradle_cache_is_pooled_by_default_when_java_stack_on(tmp_path):
    """The java stack adds caches/gradle; its preset defaults to pooled."""
    from jailbee.config import load_config_from_text

    cfg = load_config_from_text("golden:\n  stacks:\n    java: corretto-21\n", tmp_path / "c.yaml")
    gradle = next(c for c in cfg.effective_shared_caches() if c.name == "gradle")
    assert gradle.pool is not None
    assert "caches/modules-2/files-2.1" in gradle.pool.link_paths


def test_pooled_caches_false_opts_out(tmp_path):
    from jailbee.config import load_config_from_text

    cfg = load_config_from_text(
        "golden:\n  stacks:\n    java: corretto-21\npooled_caches:\n  gradle: false\n",
        tmp_path / "c.yaml",
    )
    gradle = next(c for c in cfg.effective_shared_caches() if c.name == "gradle")
    assert gradle.pool is None


def test_pooled_caches_true_opts_in_for_default_off_preset(tmp_path):
    from jailbee.config import load_config_from_text

    cfg = load_config_from_text(
        "golden:\n  stacks:\n    node: 24\npooled_caches:\n  npm: true\n",
        tmp_path / "c.yaml",
    )
    npm = next(c for c in cfg.effective_shared_caches() if c.name == "npm")
    assert npm.pool is not None
    assert npm.pool.link_paths == ["_cacache"]


def test_explicit_pool_block_beats_pooled_caches(tmp_path):
    from jailbee.config import load_config_from_text

    text = (
        "pooled_caches:\n  gradle: false\n"
        "shared_caches:\n"
        "  - name: gradle\n"
        "    host_subpath: caches/gradle\n"
        "    container_path: ~/.gradle\n"
        "    pool:\n"
        "      seed: false\n"
    )
    cfg = load_config_from_text(text, tmp_path / "c.yaml")
    gradle = next(c for c in cfg.effective_shared_caches() if c.name == "gradle")
    assert gradle.pool is not None
    assert gradle.pool.seed is False


def test_pooled_caches_unknown_name_is_an_error(tmp_path):
    from jailbee.config import ConfigError, load_config_from_text

    with pytest.raises(ConfigError, match="no shared cache named 'nosuch'"):
        load_config_from_text("pooled_caches:\n  nosuch: true\n", tmp_path / "c.yaml")


def test_pooled_caches_true_without_preset_is_an_error(tmp_path):
    from jailbee.config import ConfigError, load_config_from_text

    text = (
        "pooled_caches:\n  sbt: true\n"
        "shared_caches:\n"
        "  - name: sbt\n"
        "    host_subpath: caches/sbt\n"
        "    container_path: ~/.sbt\n"
    )
    with pytest.raises(ConfigError, match="no builtin pool preset"):
        load_config_from_text(text, tmp_path / "c.yaml")


def test_pooled_caches_false_on_a_pool_only_preset_is_an_error(tmp_path):
    """`chrome-profile`'s host_subpath is the pool root itself, so the
    un-pooled form would render `source: <shared_dir>/chrome-pool`,
    `path: ~/.config/google-chrome` — every container's Chrome profile
    becoming the pool root, writing alongside `slots/` and `by-container/`
    and thereby breaking the next `ensure_pool_dirs`."""
    from jailbee.config import ConfigError, load_config_from_text

    text = "chrome:\n  enabled: true\npooled_caches:\n  chrome-profile: false\n"
    with pytest.raises(ConfigError, match="cannot be un-pooled"):
        load_config_from_text(text, tmp_path / "c.yaml")


def test_pool_only_preset_stays_pooled_even_if_the_flag_says_otherwise(tmp_path):
    """Defence in depth for a Config built without validation: honouring
    `false` here is what mounts the pool root into the container."""
    from jailbee.config import POOL_PRESETS, load_config_from_text

    assert POOL_PRESETS["chrome-profile"].pool_only is True
    cfg = load_config_from_text("chrome:\n  enabled: true\n", tmp_path / "c.yaml")
    cfg = cfg.model_copy(update={"pooled_caches": {"chrome-profile": False}})
    chrome = next(c for c in cfg.effective_shared_caches() if c.name == "chrome-profile")
    assert chrome.pool is not None


def test_chrome_pool_entry_matches_the_legacy_layout(tmp_path):
    """The device name and pool root existing containers already carry."""
    from jailbee.config import load_config_from_text

    cfg = load_config_from_text("chrome:\n  enabled: true\n", tmp_path / "c.yaml")
    chrome = next(c for c in cfg.effective_shared_caches() if c.name == "chrome-profile")
    assert chrome.host_subpath == "chrome-pool"
    assert chrome.container_path == "~/.config/google-chrome"
    assert chrome.pool is not None
    assert chrome.pool.allocate == "on-demand"
    assert chrome.pool.warmth_file == "Default/Login Data"
