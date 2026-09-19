import pytest
from pydantic import ValidationError

from jailbee.config import (
    AgentConfig,
    ClaudeAgentConfig,
    ConfigError,
    device_name,
    resolve_agents_raw,
)
from jailbee.config.models_agents import AgentSharedMount
from tests.conftest import make_cfg


def test_device_name_maps_dots_to_dashes():
    assert device_name("claude") == "claude"
    assert device_name("claude.json") == "claude-json"
    assert device_name("claude-install") == "claude-install"


def test_preset_supplies_command_and_install():
    out = resolve_agents_raw({"agents": {"codex": {"enabled": True}}})
    codex = out["agents"]["codex"]
    assert codex["command"] == "codex"
    assert codex["install"] == (
        "curl -fsSL https://chatgpt.com/codex/install.sh | CODEX_NON_INTERACTIVE=1 sh"
    )


def test_user_scalar_overrides_preset():
    out = resolve_agents_raw({"agents": {"codex": {"enabled": True, "install": "my-installer"}}})
    assert out["agents"]["codex"]["install"] == "my-installer"


def test_user_egress_appends_to_preset():
    out = resolve_agents_raw(
        {"agents": {"codex": {"enabled": True, "egress_allow": ["extra.host:443"]}}}
    )
    hosts = out["agents"]["codex"]["egress_allow"]
    assert "api.openai.com:443" in hosts
    assert hosts[-1] == "extra.host:443"


def test_empty_egress_list_resets_preset():
    out = resolve_agents_raw({"agents": {"codex": {"egress_allow": []}}})
    assert out["agents"]["codex"]["egress_allow"] == []


def test_unknown_agent_name_gets_no_preset_base():
    out = resolve_agents_raw({"agents": {"mine": {"command": "mine"}}})
    assert out["agents"]["mine"] == {"command": "mine"}


def test_legacy_claude_block_is_translated():
    out = resolve_agents_raw({"claude": {"enabled": True, "autostart": True}})
    assert "claude" not in out
    claude = out["agents"]["claude"]
    assert claude["enabled"] is True
    assert claude["autostart"] is True
    assert claude["command"] == "claude"


def test_both_claude_spellings_is_an_error():
    with pytest.raises(ConfigError, match=r"both `claude:` and `agents.claude`"):
        resolve_agents_raw({"claude": {"enabled": True}, "agents": {"claude": {"enabled": True}}})


def test_malformed_claude_block_error_names_the_config_path(tmp_path, mocker):
    """`resolve_agents_raw` now runs inside `_build_config_from_dict`, before
    the `Config.model_validate` call — its `ConfigError` must keep the
    `Config validation failed in <path>:` wrapper too, or a malformed
    `claude:`/`agents:` block loses the file name that names the mistake."""
    from jailbee.config import load_config

    mocker.patch("jailbee.config.loader.detect_default_branch", return_value="main")
    repo = tmp_path / "r"
    (repo / ".jailbee").mkdir(parents=True)
    (repo / ".git").mkdir()
    cfg_path = repo / ".jailbee" / "config.yaml"
    cfg_path.write_text("claude: 5\n")

    with pytest.raises(ConfigError) as exc:
        load_config(cfg_path)

    msg = str(exc.value)
    assert "must be a mapping" in msg
    assert str(cfg_path) in msg


def test_malformed_agents_block_error_names_the_config_path(tmp_path, mocker):
    from jailbee.config import load_config

    mocker.patch("jailbee.config.loader.detect_default_branch", return_value="main")
    repo = tmp_path / "r"
    (repo / ".jailbee").mkdir(parents=True)
    (repo / ".git").mkdir()
    cfg_path = repo / ".jailbee" / "config.yaml"
    cfg_path.write_text("agents: 5\n")

    with pytest.raises(ConfigError) as exc:
        load_config(cfg_path)

    msg = str(exc.value)
    assert "must be a mapping" in msg
    assert str(cfg_path) in msg


def test_claude_entry_accepts_claude_only_fields():
    cfg = ClaudeAgentConfig.model_validate({"enabled": True, "ai_pr_timeout": 900})
    assert cfg.ai_pr_timeout == 900


def test_generic_agent_rejects_claude_only_fields():
    with pytest.raises(ValueError):
        AgentConfig.model_validate({"enabled": True, "ai_pr_timeout": 900})


def test_generic_agent_accepts_install_jailbee_skills():
    """The flag is agent-generic: opting codex out is the same YAML key as
    opting claude out."""
    cfg = AgentConfig.model_validate({"install_jailbee_skills": False})
    assert cfg.install_jailbee_skills is False


@pytest.mark.parametrize(
    "bad", ["", "../escape", "~/.mine/../evil/skills", "./skills", "~/.mine/./skills"]
)
def test_skills_dir_rejects_empty_and_traversal_segments(bad):
    """A repo-committed `skills_dir` must not step outside the agent's own
    mount: a `..` segment reaches `_skills_host_dir`'s textual join and lets
    the copy write (and `rmtree`) outside `<shared_dir>`."""
    with pytest.raises(ValidationError, match="skills_dir"):
        AgentConfig.model_validate({"enabled": True, "command": "mine", "skills_dir": bad})


def test_skills_dir_accepts_absolute_and_tilde_paths_with_dotfiles():
    cfg = AgentConfig.model_validate(
        {"enabled": True, "command": "mine", "skills_dir": "~/.config/my.agent/skills"}
    )
    assert cfg.skills_dir == "~/.config/my.agent/skills"
    cfg = AgentConfig.model_validate(
        {"enabled": True, "command": "mine", "skills_dir": "/opt/skills"}
    )
    assert cfg.skills_dir == "/opt/skills"


def test_presets_declare_skills_dir_only_for_skill_capable_agents():
    from jailbee.agent_presets import AGENT_PRESETS, claude_preset

    presets = {**AGENT_PRESETS, "claude": claude_preset()}
    with_dir = {name for name, preset in presets.items() if preset.get("skills_dir")}
    assert with_dir == {"claude", "codex", "gemini", "opencode"}
    # Each must land inside a mount the preset itself declares.
    assert presets["claude"]["skills_dir"] == "~/.claude/skills"
    assert presets["codex"]["skills_dir"] == "~/.codex/skills"
    assert presets["gemini"]["skills_dir"] == "~/.gemini/skills"
    assert presets["opencode"]["skills_dir"] == "~/.config/opencode/skills"


def test_install_check_defaults_from_command():
    cfg = AgentConfig.model_validate({"enabled": True, "command": "codex --yolo"})
    assert cfg.effective_install_check() == "command -v codex"


# ---------- Step 7: conflict/uniqueness tests (validate_runtime) ----------


def test_identical_shared_mount_across_agents_is_allowed(tmp_path):
    cfg = make_cfg(
        tmp_path,
        agents={
            "a": {
                "enabled": True,
                "command": "a",
                "shared": [{"subpath": "npm-global", "path": "~/.npm-global"}],
            },
            "b": {
                "enabled": True,
                "command": "b",
                "shared": [{"subpath": "npm-global", "path": "~/.npm-global"}],
            },
        },
    )
    assert cfg.validate_runtime() == []


def test_conflicting_shared_mount_across_agents_is_reported(tmp_path):
    cfg = make_cfg(
        tmp_path,
        agents={
            "a": {
                "enabled": True,
                "command": "a",
                "shared": [{"subpath": "shared", "path": "~/.a"}],
            },
            "b": {
                "enabled": True,
                "command": "b",
                "shared": [{"subpath": "shared", "path": "~/.b"}],
            },
        },
    )
    assert any("claimed twice" in i for i in cfg.validate_runtime())


def test_autostart_requires_enabled(tmp_path):
    cfg = make_cfg(tmp_path, agents={"a": {"autostart": True, "command": "a"}})
    issues = cfg.validate_runtime()
    assert any("requires agents.a.enabled" in i for i in issues)


def test_bad_agent_name_is_an_error(tmp_path):
    cfg = make_cfg(tmp_path, agents={"Not_Valid": {"enabled": True, "command": "x"}})
    issues = cfg.validate_runtime()
    assert any("must match [a-z0-9-]+" in i for i in issues)


# ---------- Task 2: Config.claude derived from agents.claude ----------


def test_claude_property_reflects_agents_entry(tmp_path):
    cfg = make_cfg(tmp_path, agents={"claude": {"enabled": True, "ai_pr_timeout": 900}})
    assert cfg.claude.enabled is True
    assert cfg.claude.ai_pr_timeout == 900


def test_claude_property_defaults_disabled_when_absent(tmp_path):
    cfg = make_cfg(tmp_path)
    assert cfg.claude.enabled is False
    assert cfg.claude.command == "claude"


def test_with_agent_helper_actually_changes_the_config(tmp_path):
    """Guards the silent-failure mode: a property shadows model_copy's dict."""
    from tests.conftest import with_agent

    cfg = with_agent(make_cfg(tmp_path), "claude", enabled=True)
    assert cfg.claude.enabled is True


def test_private_subpaths_are_accepted_on_a_dir_mount():
    m = AgentSharedMount.model_validate(
        {
            "subpath": "codex",
            "path": "~/.codex",
            "private": ["app-server-control", "app-server-daemon"],
        }
    )
    assert m.private == ["app-server-control", "app-server-daemon"]


def test_private_defaults_to_empty():
    m = AgentSharedMount.model_validate({"subpath": "codex", "path": "~/.codex"})
    assert m.private == []


def test_private_is_rejected_on_a_file_mount():
    with pytest.raises(ValidationError, match="private is only valid for type: dir"):
        AgentSharedMount.model_validate(
            {
                "subpath": "aider.conf.yml",
                "path": "~/.aider.conf.yml",
                "type": "file",
                "private": ["nope"],
            }
        )


@pytest.mark.parametrize("bad", ["", "/abs", "../escape", "a/../../b", "."])
def test_private_rejects_paths_that_escape_the_mount(bad):
    with pytest.raises(ValidationError, match="private subpath"):
        AgentSharedMount.model_validate({"subpath": "codex", "path": "~/.codex", "private": [bad]})


def test_codex_preset_keeps_the_app_server_dirs_per_container():
    """The socket in app-server-control lets one container's Codex frontend
    drive another container's daemon, which then resolves the working
    directory against its own rootfs and edits the wrong clone."""
    from jailbee.agent_presets import AGENT_PRESETS

    shared = AGENT_PRESETS["codex"]["shared"]
    assert isinstance(shared, list)
    assert shared[0]["private"] == ["app-server-control", "app-server-daemon"]


def _run_opencode_step(which, tmp_path, *, installer_body):
    """Run the opencode preset's install/update line in a real bash.

    That line is the only thing standing between "the vendor installer ran" and
    "`command -v opencode` works", and none of its logic is Python — so it is
    exercised as shell. No network: `curl` is a stub on PATH that prints
    `installer_body`, which the preset then pipes into `bash -s --`.

    Returns the completed process, the fake HOME, and how many times the stub
    curl was called.
    """
    import os
    import subprocess

    from jailbee.agent_presets import AGENT_PRESETS

    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    stub_bin = tmp_path / "stub-bin"
    stub_bin.mkdir()
    installer = tmp_path / "installer.sh"
    installer.write_text(installer_body)
    curl_log = tmp_path / "curl.log"
    curl = stub_bin / "curl"
    curl.write_text(f'#!/bin/sh\necho called >> "{curl_log}"\ncat "{installer}"\n')
    curl.chmod(0o755)

    command = AGENT_PRESETS["opencode"][which]
    assert isinstance(command, str)
    result = subprocess.run(
        ["bash", "-c", command],
        env={"HOME": str(home), "PATH": f"{stub_bin}:{os.environ['PATH']}"},
        capture_output=True,
        text=True,
        check=False,
    )
    calls = curl_log.read_text().count("called") if curl_log.exists() else 0
    return result, home, calls


# Stands in for https://opencode.ai/v2/install: all this test cares about is
# that it drops an executable at the hardcoded INSTALL_DIR the real one uses.
_FAKE_OPENCODE_INSTALLER = (
    'mkdir -p "$HOME/.opencode/bin"\n'
    'printf "#!/bin/sh\\n" > "$HOME/.opencode/bin/opencode"\n'
    'chmod 755 "$HOME/.opencode/bin/opencode"\n'
)


def _seed_shared_binary(tmp_path):
    binary = tmp_path / "home/.opencode/bin/opencode"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    return binary


def test_opencode_install_links_the_binary_onto_path(tmp_path):
    """The installer hardcodes ~/.opencode/bin, which is on no PATH jailbee
    sets — without the link `command -v opencode` fails, so every `jailbee new`
    reinstalls and the autostart window dies with `opencode: not found`."""
    result, home, calls = _run_opencode_step(
        "install", tmp_path, installer_body=_FAKE_OPENCODE_INSTALLER
    )

    assert result.returncode == 0, result.stderr
    assert calls == 1
    link = home / ".local/bin/opencode"
    assert link.is_symlink()
    assert link.resolve() == home / ".opencode/bin/opencode"


def test_opencode_install_skips_the_download_when_the_shared_store_has_it(tmp_path):
    """~/.opencode is shared across a repo's containers, so a second branch
    must relink rather than re-fetch the 88MB tarball. The stub installer here
    fails outright: reaching it at all is the bug."""
    _seed_shared_binary(tmp_path)

    result, home, calls = _run_opencode_step("install", tmp_path, installer_body="exit 1\n")

    assert result.returncode == 0, result.stderr
    assert calls == 0
    assert (home / ".local/bin/opencode").is_symlink()


def test_opencode_update_always_reruns_the_installer(tmp_path):
    """Unlike install, update has no already-present short-circuit — rerunning
    the installer is the whole of how opencode upgrades."""
    _seed_shared_binary(tmp_path)

    result, home, calls = _run_opencode_step(
        "update", tmp_path, installer_body=_FAKE_OPENCODE_INSTALLER
    )

    assert result.returncode == 0, result.stderr
    assert calls == 1
    assert (home / ".local/bin/opencode").is_symlink()


def test_opencode_install_fails_loudly_when_the_installer_produces_nothing(tmp_path):
    """`curl … | bash` exits 0 when curl fails — bash just reads an empty
    script. Without the trailing `-x` test a failed download would be reported
    as a successful install step and only surface later as `opencode: not
    found` in the autostart window."""
    result, home, _calls = _run_opencode_step("install", tmp_path, installer_body="")

    assert result.returncode != 0
    assert not (home / ".local/bin/opencode").exists()
