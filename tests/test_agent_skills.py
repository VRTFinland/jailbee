from __future__ import annotations

from pathlib import Path

from jailbee import agent_skills
from tests.conftest import make_config


def _fake_skills_root(tmp_path: Path) -> Path:
    """Build a synthetic bundled-skills tree with two skills.

    Idempotent: `_copy_skills_into` re-resolves `_skills_root()` once per
    destination, so a multi-agent sync builds this tree several times.
    """
    root = tmp_path / "bundled-skills"
    usage = root / "jailbee-usage"
    usage.mkdir(parents=True, exist_ok=True)
    (usage / "SKILL.md").write_text("usage skill\n")
    (usage / "references").mkdir(exist_ok=True)
    (usage / "references" / "commands.md").write_text("commands\n")
    setup = root / "jailbee-repo-setup"
    setup.mkdir(parents=True, exist_ok=True)
    (setup / "SKILL.md").write_text("setup skill\n")
    return root


def test_skills_root_dev_fallback_is_docs_skills() -> None:
    # In the editable/dev checkout there is no packaged jailbee/skills,
    # so the helper must resolve to the repo's docs/skills (which exists here).
    root = agent_skills._skills_root()
    assert root.name == "skills"
    assert (root / "jailbee-usage" / "SKILL.md").is_file()


def test_sync_noop_when_claude_disabled(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(agent_skills, "_skills_root", lambda: _fake_skills_root(tmp_path))
    shared = tmp_path / "shared"
    cfg = make_config(tmp_path / "repo", shared_dir=shared, claude={"enabled": False})
    agent_skills.sync_agent_skills(cfg)
    assert not (shared / "claude" / "skills").exists()
    # No agent wanted skills, so no lock is created either.
    assert not (shared / ".jailbee-skills.lock").exists()


def test_sync_noop_when_install_flag_off(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(agent_skills, "_skills_root", lambda: _fake_skills_root(tmp_path))
    shared = tmp_path / "shared"
    cfg = make_config(
        tmp_path / "repo",
        shared_dir=shared,
        claude={"enabled": True, "install_jailbee_skills": False},
    )
    agent_skills.sync_agent_skills(cfg)
    assert not (shared / "claude" / "skills").exists()


def test_sync_copies_all_skills(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(agent_skills, "_skills_root", lambda: _fake_skills_root(tmp_path))
    shared = tmp_path / "shared"
    cfg = make_config(tmp_path / "repo", shared_dir=shared, claude={"enabled": True})
    agent_skills.sync_agent_skills(cfg)
    skills = shared / "claude" / "skills"
    assert (skills / "jailbee-usage" / "SKILL.md").read_text() == "usage skill\n"
    assert (skills / "jailbee-usage" / "references" / "commands.md").read_text() == "commands\n"
    assert (skills / "jailbee-repo-setup" / "SKILL.md").read_text() == "setup skill\n"


def test_sync_removes_stale_files_in_managed_skill(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(agent_skills, "_skills_root", lambda: _fake_skills_root(tmp_path))
    shared = tmp_path / "shared"
    cfg = make_config(tmp_path / "repo", shared_dir=shared, claude={"enabled": True})
    skills = shared / "claude" / "skills"
    stale = skills / "jailbee-usage"
    stale.mkdir(parents=True)
    (stale / "OLD.md").write_text("deleted upstream\n")
    agent_skills.sync_agent_skills(cfg)
    assert not (skills / "jailbee-usage" / "OLD.md").exists()
    assert (skills / "jailbee-usage" / "SKILL.md").is_file()


def test_bundled_skills_include_pr_review() -> None:
    assert "jailbee-pr-review" in agent_skills.bundled_skill_names()


def test_pr_review_skill_forbids_writing_from_the_container() -> None:
    text = (Path(agent_skills._skills_root()) / "jailbee-pr-review" / "SKILL.md").read_text()
    # The whole point of the outbox: the container never mutates GitHub.
    assert "gh pr comment" in text
    assert "gh pr review" in text
    assert "jb review apply" in text
    assert "~/.jailbee/pr-outbox" in text


def test_sync_leaves_unrelated_skills_untouched(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(agent_skills, "_skills_root", lambda: _fake_skills_root(tmp_path))
    shared = tmp_path / "shared"
    cfg = make_config(tmp_path / "repo", shared_dir=shared, claude={"enabled": True})
    skills = shared / "claude" / "skills"
    other = skills / "my-own-skill"
    other.mkdir(parents=True)
    (other / "SKILL.md").write_text("mine\n")
    agent_skills.sync_agent_skills(cfg)
    assert (other / "SKILL.md").read_text() == "mine\n"


# --------------------------------------------------------------------------
# the multi-agent sync
# --------------------------------------------------------------------------


def test_sync_copies_to_every_enabled_agents_skills_dir(tmp_path: Path, monkeypatch) -> None:
    """Claude, codex, gemini and opencode each get the skills in their own
    shared skills directory — the mount subpath each preset declares."""
    monkeypatch.setattr(agent_skills, "_skills_root", lambda: _fake_skills_root(tmp_path))
    shared = tmp_path / "shared"
    cfg = make_config(
        tmp_path / "repo",
        shared_dir=shared,
        agents={
            "claude": {"enabled": True},
            "codex": {"enabled": True},
            "gemini": {"enabled": True},
            "opencode": {"enabled": True},
        },
    )
    agent_skills.sync_agent_skills(cfg)
    # opencode's ~/.config/opencode mount is the "opencode-config" subpath.
    for subpath in ("claude", "codex", "gemini", "opencode-config"):
        skills = shared / subpath / "skills"
        assert (skills / "jailbee-usage" / "SKILL.md").read_text() == "usage skill\n"
        assert (skills / "jailbee-repo-setup" / "SKILL.md").is_file()


def test_sync_skips_a_disabled_agent(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(agent_skills, "_skills_root", lambda: _fake_skills_root(tmp_path))
    shared = tmp_path / "shared"
    cfg = make_config(
        tmp_path / "repo",
        shared_dir=shared,
        agents={
            "claude": {"enabled": True},
            "codex": {"enabled": False},
        },
    )
    agent_skills.sync_agent_skills(cfg)
    assert (shared / "claude" / "skills" / "jailbee-usage").is_dir()
    assert not (shared / "codex" / "skills").exists()


def test_sync_honours_the_per_agent_install_flag(tmp_path: Path, monkeypatch) -> None:
    """`install_jailbee_skills: false` opts one agent out, not everyone."""
    monkeypatch.setattr(agent_skills, "_skills_root", lambda: _fake_skills_root(tmp_path))
    shared = tmp_path / "shared"
    cfg = make_config(
        tmp_path / "repo",
        shared_dir=shared,
        agents={
            "claude": {"enabled": True},
            "codex": {"enabled": True, "install_jailbee_skills": False},
        },
    )
    agent_skills.sync_agent_skills(cfg)
    assert (shared / "claude" / "skills" / "jailbee-usage").is_dir()
    assert not (shared / "codex" / "skills").exists()


def test_sync_skips_agents_without_a_skills_dir(tmp_path: Path, monkeypatch) -> None:
    """aider and grok have no skills mechanism; enabling them owes nothing."""
    monkeypatch.setattr(agent_skills, "_skills_root", lambda: _fake_skills_root(tmp_path))
    shared = tmp_path / "shared"
    cfg = make_config(
        tmp_path / "repo",
        shared_dir=shared,
        agents={"aider": {"enabled": True}, "grok": {"enabled": True}},
    )
    agent_skills.sync_agent_skills(cfg)
    assert not (shared / "aider" / "skills").exists()
    assert not (shared / "grok" / "skills").exists()
    assert not (shared / ".jailbee-skills.lock").exists()


def test_sync_follows_a_custom_agents_own_mount(tmp_path: Path, monkeypatch) -> None:
    """A from-scratch agent gets its skills wherever its own mount says."""
    monkeypatch.setattr(agent_skills, "_skills_root", lambda: _fake_skills_root(tmp_path))
    shared = tmp_path / "shared"
    cfg = make_config(
        tmp_path / "repo",
        shared_dir=shared,
        agents={
            "mine": {
                "enabled": True,
                "command": "mine",
                "skills_dir": "~/.mine/skills",
                "shared": [{"subpath": "my-agent", "path": "~/.mine"}],
            }
        },
    )
    agent_skills.sync_agent_skills(cfg)
    assert (shared / "my-agent" / "skills" / "jailbee-usage" / "SKILL.md").is_file()


def test_sync_warns_when_no_shared_mount_covers_skills_dir(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """A skills_dir nothing mounts is a config mistake, not a crash: warn,
    skip that agent, still serve the others."""
    monkeypatch.setattr(agent_skills, "_skills_root", lambda: _fake_skills_root(tmp_path))
    shared = tmp_path / "shared"
    cfg = make_config(
        tmp_path / "repo",
        shared_dir=shared,
        agents={
            "claude": {"enabled": True},
            "mine": {
                "enabled": True,
                "command": "mine",
                "skills_dir": "~/nowhere/skills",
                "shared": [{"subpath": "mine", "path": "~/.mine"}],
            },
        },
    )
    agent_skills.sync_agent_skills(cfg)
    out = capsys.readouterr().out
    assert "mine" in out
    assert "~/nowhere/skills" in out
    assert (shared / "claude" / "skills" / "jailbee-usage").is_dir()


def test_sync_lock_lives_at_the_shared_dir_root(tmp_path: Path, monkeypatch) -> None:
    """One lock serialises every agent's copy, so it sits beside the
    `.agent-install.lock`, not inside one agent's mount."""
    monkeypatch.setattr(agent_skills, "_skills_root", lambda: _fake_skills_root(tmp_path))
    shared = tmp_path / "shared"
    cfg = make_config(
        tmp_path / "repo",
        shared_dir=shared,
        agents={"claude": {"enabled": True}, "codex": {"enabled": True}},
    )
    agent_skills.sync_agent_skills(cfg)
    assert (shared / ".jailbee-skills.lock").exists()
    assert not (shared / "claude" / ".jailbee-skills.lock").exists()


# --------------------------------------------------------------------------
# the host-side install
# --------------------------------------------------------------------------


def test_host_skill_targets_follow_installed_binaries(tmp_path: Path, monkeypatch, mocker) -> None:
    """Detection is `shutil.which` per preset binary: an agent the user has
    not installed on the host owes no skills directory."""
    monkeypatch.setenv("HOME", str(tmp_path))
    mocker.patch("shutil.which", side_effect=lambda b: f"/usr/bin/{b}" if b == "claude" else None)

    targets = agent_skills.host_skill_targets()

    assert targets == [tmp_path / ".claude" / "skills"]


def test_install_host_skills_writes_every_given_target(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(agent_skills, "_skills_root", lambda: _fake_skills_root(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))

    written = agent_skills.install_host_skills(
        [tmp_path / ".claude" / "skills", tmp_path / ".codex" / "skills"]
    )

    assert (tmp_path / ".claude" / "skills" / "jailbee-usage" / "SKILL.md").is_file()
    assert (tmp_path / ".codex" / "skills" / "jailbee-usage" / "SKILL.md").is_file()
    assert len(written) == 4  # two skills, two targets
