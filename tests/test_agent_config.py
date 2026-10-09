import pytest
from pydantic import ValidationError

from jailbee.config import (
    AgentConfig,
    ClaudeAgentConfig,
    ConfigError,
    device_name,
    resolve_agents_raw,
)
from jailbee.config.models_agents import AgentGlobalInstructions, AgentSharedMount
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
    cfg = ClaudeAgentConfig.model_validate({"enabled": True, "agent_view": True})
    assert cfg.agent_view is True


def test_generic_agent_rejects_claude_only_fields():
    with pytest.raises(ValueError):
        AgentConfig.model_validate({"enabled": True, "agent_view": True})


def test_generic_agent_accepts_install_jailbee_skills():
    """The flag is agent-generic: opting codex out is the same YAML key as
    opting claude out."""
    cfg = AgentConfig.model_validate({"install_jailbee_skills": False})
    assert cfg.install_jailbee_skills is False


def test_global_instructions_defaults_to_none():
    assert AgentConfig(command="x").global_instructions is None


@pytest.mark.parametrize(
    "bad_dir", ["", "relative/dir", "/etc/../root", "/etc/./x", "~/../x", "/etc/a\x00b"]
)
def test_global_instructions_rejects_bad_dir(bad_dir):
    with pytest.raises(ValidationError):
        AgentGlobalInstructions(dir=bad_dir, file="CLAUDE.md")


@pytest.mark.parametrize("bad_file", ["", ".", "..", "a/b", "/CLAUDE.md", "a\x00b"])
def test_global_instructions_rejects_non_bare_file(bad_file):
    with pytest.raises(ValidationError):
        AgentGlobalInstructions(dir="/etc/claude-code", file=bad_file)


def test_global_instructions_accepts_tilde_dir():
    gi = AgentGlobalInstructions(dir="~/.config/agent-policy", file="AGENTS.md")
    assert gi.dir == "~/.config/agent-policy"


@pytest.mark.parametrize("bad_dir", ["~/.claude", "~/.claude/policy"])
def test_global_instructions_dir_may_not_sit_in_a_shared_mount(bad_dir):
    with pytest.raises(ValidationError, match="shared"):
        AgentConfig(
            command="claude",
            shared=[{"subpath": "claude", "path": "~/.claude"}],
            global_instructions={"dir": bad_dir, "file": "CLAUDE.md"},
        )


@pytest.mark.parametrize("bad_dir", ["/", "/etc", "/etc/", "/usr", "/home", "~", "/home/dev"])
def test_global_instructions_dir_may_not_be_a_system_or_home_directory(bad_dir):
    with pytest.raises(ValidationError, match="too broad"):
        AgentConfig(
            command="claude",
            global_instructions={"dir": bad_dir, "file": "CLAUDE.md"},
        )


def test_global_instructions_dir_may_not_contain_a_shared_mount():
    with pytest.raises(ValidationError, match="overlaps the shared mount"):
        AgentConfig(
            command="claude",
            shared=[{"subpath": "state", "path": "/opt/agent/state"}],
            global_instructions={"dir": "/opt/agent", "file": "CLAUDE.md"},
        )


def test_global_instructions_dir_beside_a_shared_mount_is_fine():
    cfg = AgentConfig(
        command="claude",
        shared=[{"subpath": "claude", "path": "~/.claude"}],
        global_instructions={"dir": "~/.claude-policy", "file": "CLAUDE.md"},
    )
    assert cfg.global_instructions is not None


@pytest.mark.parametrize(
    ("shared_path", "instructions_dir"),
    [
        ("~/.claude", "/home/dev/.claude/policy"),
        ("/home/dev/.claude", "~/.claude/policy"),
    ],
)
def test_global_instructions_rejects_mixed_home_path_forms(shared_path, instructions_dir):
    with pytest.raises(ValidationError, match="shared"):
        AgentConfig(
            command="claude",
            shared=[{"subpath": "claude", "path": shared_path}],
            global_instructions={"dir": instructions_dir, "file": "CLAUDE.md"},
        )


def test_claude_preset_declares_etc_claude_code():
    from jailbee.agent_presets import claude_preset

    assert claude_preset()["global_instructions"] == {
        "dir": "/etc/claude-code",
        "file": "CLAUDE.md",
    }


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
    assert with_dir == {"claude", "codex", "gemini", "opencode", "pi"}
    # Each must land inside a mount the preset itself declares.
    assert presets["claude"]["skills_dir"] == "~/.claude/skills"
    assert presets["codex"]["skills_dir"] == "~/.codex/skills"
    assert presets["gemini"]["skills_dir"] == "~/.gemini/skills"
    assert presets["opencode"]["skills_dir"] == "~/.config/opencode/skills"
    assert presets["pi"]["skills_dir"] == "~/.pi/agent/skills"


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
    cfg = make_cfg(tmp_path, agents={"claude": {"enabled": True, "agent_view": True}})
    assert cfg.claude.enabled is True
    assert cfg.claude.agent_view is True


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


def test_claude_preset_keeps_its_runtime_state_per_container():
    """`sessions/` is Claude Code's live-session registry; `daemon/` and `jobs/`
    are the background daemon's roster, dispatch queue and job state. Shared,
    a daemon in one container adopts, runs twice or declares dead another
    container's jobs."""
    from jailbee.agent_presets import claude_preset

    shared = claude_preset()["shared"]
    assert isinstance(shared, list)
    assert shared[0]["subpath"] == "claude"
    assert shared[0]["private"] == ["sessions", "daemon", "jobs"]


def test_opencode_preset_shares_no_session_or_auth_state():
    """opencode keeps its whole session, auth tokens included, in a SQLite
    database under its config/data homes. Sharing either across a repo's
    containers would hand each container the others' credentials, so only the
    binary store and the skills directory may be shared."""
    from jailbee.agent_presets import AGENT_PRESETS

    shared = AGENT_PRESETS["opencode"]["shared"]
    assert isinstance(shared, list)
    paths = {m["path"] for m in shared}

    assert paths == {"~/.opencode", "~/.config/opencode/skills"}


def test_opencode_installs_and_updates_through_one_script():
    """With a per-container launcher every fresh container takes `install`, so
    the update decision has to live in the script. ensure-opencode.sh's
    behaviour is in test_provision_ensure_opencode.py."""
    from jailbee.agent_presets import AGENT_PRESETS

    assert AGENT_PRESETS["opencode"]["install"] == "__bundled__:ensure-opencode.sh"
    assert AGENT_PRESETS["opencode"]["update"] == "__bundled__:ensure-opencode.sh"


def test_pi_preset_shares_the_agent_home_and_the_install_store():
    from jailbee.agent_presets import AGENT_PRESETS

    shared = AGENT_PRESETS["pi"]["shared"]
    assert isinstance(shared, list)

    assert {m["subpath"]: m["path"] for m in shared} == {
        "pi": "~/.pi/agent",
        "pi-install": "~/.local/share/pi",
    }


def test_pi_installs_and_updates_through_one_script():
    """With a per-container launcher every fresh container takes `install`, so
    the update decision has to live in the script, not in the install/update
    split. ensure-pi.sh's behaviour is in test_provision_ensure_pi.py."""
    from jailbee.agent_presets import AGENT_PRESETS

    assert AGENT_PRESETS["pi"]["install"] == "__bundled__:ensure-pi.sh"
    assert AGENT_PRESETS["pi"]["update"] == "__bundled__:ensure-pi.sh"
