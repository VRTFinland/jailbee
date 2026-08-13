"""Tests for `gie config init` template generation."""

import os

import pytest
import yaml
from typer.testing import CliRunner

from jailbee.cli import app
from jailbee.config import load_config
from jailbee.config_init import (
    render_global_template,
    render_template,
    write_global_template,
    write_template,
)

runner = CliRunner()


def test_render_template_omits_per_developer_keys(tmp_path):
    """The repo template must not bake in per-developer state.

    `container_user.uid/gid` lives in ~/.config/gie/global.yaml; `shared_dir`
    is auto-derived from the repo path at load time. Neither should appear
    in `.gie/config.yaml`, which is checked into the repo and shared across
    the dev team.
    """
    out = render_template(repo_root=tmp_path / "myrepo")

    # No per-user UID/GID in the rendered file
    assert str(os.getuid()) not in out
    # No host-specific shared_dir path
    assert "myrepo" not in out

    parsed = yaml.safe_load(out)
    assert "container_user" not in parsed
    assert "shared_dir" not in parsed


def test_render_template_is_valid_yaml(tmp_path):
    out = render_template(repo_root=tmp_path / "myrepo")
    parsed = yaml.safe_load(out)

    assert isinstance(parsed, dict)
    # A few stable keys that should remain in the repo template
    assert "golden" in parsed
    assert "autostart" in parsed


def test_template_round_trips_to_valid_config(tmp_path, mocker):
    mocker.patch("jailbee.config.detect_default_branch", return_value="main")
    repo_root = tmp_path / "myrepo"

    write_template(repo_root)

    cfg = load_config(repo_root / ".jailbee" / "config.yaml")

    assert cfg.repo_root == repo_root
    assert cfg.container_user.uid == os.getuid()


def test_write_template_creates_jailbee_dir_in_a_fresh_repo(tmp_path):
    path = write_template(tmp_path)

    assert path == tmp_path / ".jailbee" / "config.yaml"
    assert not (tmp_path / ".gie").exists()


def test_write_template_rewrites_in_place_for_a_legacy_repo(tmp_path):
    (tmp_path / ".gie").mkdir()
    (tmp_path / ".gie" / "config.yaml").write_text("old: true\n")

    path = write_template(tmp_path, force=True)

    assert path == tmp_path / ".gie" / "config.yaml"
    assert not (tmp_path / ".jailbee").exists()


def test_write_template_prefers_jailbee_when_both_dirs_exist(tmp_path):
    for name in (".gie", ".jailbee"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "config.yaml").write_text("old: true\n")

    path = write_template(tmp_path, force=True)

    assert path == tmp_path / ".jailbee" / "config.yaml"


def test_write_template_refuses_when_file_exists(tmp_path):
    repo_root = tmp_path / "myrepo"
    (repo_root / ".gie").mkdir(parents=True)
    cfg_path = repo_root / ".gie" / "config.yaml"
    cfg_path.write_text("existing: content\n")

    with pytest.raises(FileExistsError):
        write_template(repo_root)


def test_write_template_force_overwrites(tmp_path):
    repo_root = tmp_path / "myrepo"
    (repo_root / ".gie").mkdir(parents=True)
    cfg_path = repo_root / ".gie" / "config.yaml"
    cfg_path.write_text("existing: content\n")

    write_template(repo_root, force=True)

    assert "golden:" in cfg_path.read_text()


def test_cli_config_init_in_empty_dir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["config", "init"])

    assert result.exit_code == 0
    assert (tmp_path / ".jailbee" / "config.yaml").is_file()


def test_cli_config_init_refuses_existing_without_force(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".gie").mkdir()
    (tmp_path / ".gie" / "config.yaml").write_text("existing: stuff\n")

    result = runner.invoke(app, ["config", "init"])

    assert result.exit_code == 1
    combined = (result.stdout + (result.stderr or "")).lower()
    assert "exists" in combined
    assert "--force" in combined


def test_cli_config_init_force_overwrites(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".gie").mkdir()
    (tmp_path / ".gie" / "config.yaml").write_text("existing: stuff\n")

    result = runner.invoke(app, ["config", "init", "--force"])

    assert result.exit_code == 0
    content = (tmp_path / ".gie" / "config.yaml").read_text()
    assert "golden:" in content
    assert "existing" not in content


# --- Task 4: global template -------------------------------------------------


def test_render_global_template_substitutes_user_vars():
    out = render_global_template()

    assert str(os.getuid()) in out
    assert str(os.getgid()) in out


def test_render_global_template_is_valid_yaml():
    out = render_global_template()
    parsed = yaml.safe_load(out)

    assert isinstance(parsed, dict)
    assert parsed["container_user"]["uid"] == os.getuid()
    assert parsed["container_user"]["gid"] == os.getgid()
    assert parsed["jetbrains"]["ide"] == "idea"
    # api.anthropic.com is no longer hardcoded — the claude block auto-adds
    # it via effective_egress_allow() when claude.enabled.
    assert all(not e.startswith("api.anthropic.com") for e in parsed["egress_allow"])


def test_write_global_template_refuses_when_file_exists(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    target = tmp_path / ".config" / "jailbee" / "global.yaml"
    target.parent.mkdir(parents=True)
    target.write_text("existing: stuff\n")

    with pytest.raises(FileExistsError):
        write_global_template()


def test_write_global_template_force_overwrites(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    target = tmp_path / ".config" / "jailbee" / "global.yaml"
    target.parent.mkdir(parents=True)
    target.write_text("existing: stuff\n")

    written = write_global_template(force=True)

    assert written == target
    assert "container_user" in target.read_text()


def test_write_global_template_creates_parents(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    written = write_global_template()

    assert written == tmp_path / ".config" / "jailbee" / "global.yaml"
    assert written.is_file()


def test_global_template_contains_claude_block():
    """The global template documents the claude integration block."""
    from jailbee.config_init import render_global_template

    text = render_global_template()
    assert "claude:" in text
    assert "enabled: true" in text  # the example value
    assert "seed_from_host:" in text


# --- Task 5: --global CLI flag -----------------------------------------------


def test_cli_config_init_global_writes_global(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["config", "init", "--global"])

    assert result.exit_code == 0
    assert (tmp_path / ".config" / "jailbee" / "global.yaml").is_file()
    # Repo config NOT written by --global
    assert not (tmp_path / ".jailbee" / "config.yaml").exists()


def test_cli_config_init_global_refuses_existing_without_force(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    target = tmp_path / ".config" / "jailbee" / "global.yaml"
    target.parent.mkdir(parents=True)
    target.write_text("existing: stuff\n")

    result = runner.invoke(app, ["config", "init", "--global"])

    assert result.exit_code == 1
    combined = (result.stdout + (result.stderr or "")).lower()
    assert "exists" in combined
    assert "--force" in combined


def test_cli_config_init_global_force_overwrites(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    target = tmp_path / ".config" / "jailbee" / "global.yaml"
    target.parent.mkdir(parents=True)
    target.write_text("existing: stuff\n")

    result = runner.invoke(app, ["config", "init", "--global", "--force"])

    assert result.exit_code == 0
    assert "container_user" in target.read_text()


def test_cli_config_init_hints_about_global_when_missing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))

    result = runner.invoke(app, ["config", "init"])

    assert result.exit_code == 0
    assert (tmp_path / ".jailbee" / "config.yaml").is_file()
    combined = result.stdout + (result.stderr or "")
    assert "global.yaml" in combined or "--global" in combined


def test_cli_config_init_no_hint_when_global_exists(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    # Pre-create a global config so the hint should be suppressed
    (tmp_path / ".config" / "jailbee").mkdir(parents=True)
    (tmp_path / ".config" / "jailbee" / "global.yaml").write_text("jetbrains:\n  ide: idea\n")

    result = runner.invoke(app, ["config", "init"])

    assert result.exit_code == 0
    combined = result.stdout + (result.stderr or "")
    assert "--global" not in combined


# --- Task 6: slim repo template ----------------------------------------------


def test_repo_template_omits_personal_host_mounts_default(tmp_path):
    out = render_template(repo_root=tmp_path / "myrepo")
    parsed = yaml.safe_load(out)

    # The default repo template should not pre-fill personal mounts —
    # they belong in ~/.config/gie/global.yaml.
    mounts = parsed.get("host_mounts") or []
    containers = [m.get("container") for m in mounts]
    assert "/home/dev/.gnupg" not in containers
    assert "/home/dev/.gitconfig" not in containers
    assert "/opt/jetbrains-toolbox" not in containers


# --- Task 15: _GLOBAL_TEMPLATE uses new block API ----------------------------


def test_global_template_round_trips_through_load_config(tmp_path, monkeypatch, mocker):
    """Rendered global template must parse cleanly (no retired keys)."""
    import yaml as yaml_mod

    from jailbee.config import _build_config_from_dict, _check_retired_keys

    mocker.patch("jailbee.config.detect_default_branch", return_value="main")
    text = render_global_template()
    raw = yaml_mod.safe_load(text)
    _check_retired_keys(raw)  # must not raise

    repo_root = tmp_path / "myrepo"
    cfg_path = repo_root / ".gie" / "config.yaml"
    cfg_path.parent.mkdir(parents=True)
    cfg = _build_config_from_dict(raw, cfg_path)
    assert cfg.gpg.enabled is True
    assert cfg.ssh.enabled is True
    assert cfg.jetbrains.ide == "idea"
    assert cfg.chrome.dark_mode is False


def test_global_template_documents_all_new_blocks():
    text = render_global_template()
    assert "gpg:" in text
    assert "ssh:" in text
    assert "jetbrains:" in text
    assert "chrome:" in text
    assert "terminal:" in text
    assert "kitty:" in text
    # Comments explain behaviour
    assert "SSH_AUTH_SOCK" in text
    assert "license" in text.lower()
    assert "xterm-kitty" in text


# --- Task 16: _REPO_TEMPLATE does not set retired keys -----------------------


def test_repo_template_does_not_set_retired_keys(tmp_path):
    text = render_template(repo_root=tmp_path / "myrepo")
    for retired in (
        "\nide:",
        "chrome_url:",
        "seed_ssh_from_host:",
        "jetbrains_userprefs_from_host:",
        "open_ide:",
        "open_chrome:",
        "chrome_dark_mode:",
    ):
        assert retired not in text, f"Retired key {retired!r} still in template"


# --- github block in _GLOBAL_TEMPLATE ----------------------------------------


def test_global_template_contains_github_block():
    text = render_global_template()
    assert "github:" in text
    assert "api_tokens:" in text
    # Encourage fine-grained PATs in the example
    assert "github_pat_" in text


def test_global_template_github_block_is_disabled_by_default():
    """Template ships with enabled: false so the file loads cleanly without
    forcing the user to populate api_tokens before first use."""
    parsed = yaml.safe_load(render_global_template())
    assert parsed["github"]["enabled"] is False


def test_global_template_repo_template_does_not_contain_github(tmp_path):
    """Placement constraint: github block belongs only in global.yaml,
    so the repo template must not mention it."""
    text = render_template(repo_root=tmp_path / "myrepo")
    assert "github:" not in text


def test_global_template_github_roundtrips_through_schema():
    from jailbee.config import GithubConfig

    parsed = yaml.safe_load(render_global_template())
    # Must not raise.
    GithubConfig.model_validate(parsed["github"])


def test_global_template_writes_autostart_false_by_default():
    """The rendered ~/.config/gie/global.yaml ships with autostart off so
    opt-in to the integration does not imply auto-launch."""
    parsed = yaml.safe_load(render_global_template())
    assert parsed["jetbrains"]["autostart"] is False
    assert parsed["chrome"]["autostart"] is False


def test_repo_template_omits_host_specific_defaults_block(tmp_path):
    """memory/cpu/storage_pool are host-specific; gie config init must not
    seed them into the team-shared repo config. network defaults to strict
    via the schema, no need to spell it out either."""
    out = render_template(repo_root=tmp_path / "myrepo")
    parsed = yaml.safe_load(out)
    assert "defaults" not in parsed
    for token in ("memory:", "16GiB", "storage_pool:"):
        assert token not in out, f"{token!r} still in repo template"
