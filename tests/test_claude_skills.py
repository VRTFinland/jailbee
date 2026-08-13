from __future__ import annotations

from pathlib import Path

from jailbee import claude_skills
from tests.conftest import make_config


def _fake_skills_root(tmp_path: Path) -> Path:
    """Build a synthetic bundled-skills tree with two skills."""
    root = tmp_path / "bundled-skills"
    usage = root / "jailbee-usage"
    usage.mkdir(parents=True)
    (usage / "SKILL.md").write_text("usage skill\n")
    (usage / "references").mkdir()
    (usage / "references" / "commands.md").write_text("commands\n")
    setup = root / "jailbee-repo-setup"
    setup.mkdir(parents=True)
    (setup / "SKILL.md").write_text("setup skill\n")
    return root


def test_skills_root_dev_fallback_is_docs_skills() -> None:
    # In the editable/dev checkout there is no packaged jailbee/skills,
    # so the helper must resolve to the repo's docs/skills (which exists here).
    root = claude_skills._skills_root()
    assert root.name == "skills"
    assert (root / "jailbee-usage" / "SKILL.md").is_file()


def test_sync_noop_when_claude_disabled(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(claude_skills, "_skills_root", lambda: _fake_skills_root(tmp_path))
    shared = tmp_path / "shared"
    cfg = make_config(tmp_path / "repo", shared_dir=shared, claude={"enabled": False})
    claude_skills.sync_jailbee_skills(cfg)
    assert not (shared / "claude" / "skills").exists()


def test_sync_noop_when_install_flag_off(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(claude_skills, "_skills_root", lambda: _fake_skills_root(tmp_path))
    shared = tmp_path / "shared"
    cfg = make_config(
        tmp_path / "repo",
        shared_dir=shared,
        claude={"enabled": True, "install_jailbee_skills": False},
    )
    claude_skills.sync_jailbee_skills(cfg)
    assert not (shared / "claude" / "skills").exists()


def test_sync_copies_all_skills(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(claude_skills, "_skills_root", lambda: _fake_skills_root(tmp_path))
    shared = tmp_path / "shared"
    cfg = make_config(tmp_path / "repo", shared_dir=shared, claude={"enabled": True})
    claude_skills.sync_jailbee_skills(cfg)
    skills = shared / "claude" / "skills"
    assert (skills / "jailbee-usage" / "SKILL.md").read_text() == "usage skill\n"
    assert (skills / "jailbee-usage" / "references" / "commands.md").read_text() == "commands\n"
    assert (skills / "jailbee-repo-setup" / "SKILL.md").read_text() == "setup skill\n"


def test_sync_removes_stale_files_in_managed_skill(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(claude_skills, "_skills_root", lambda: _fake_skills_root(tmp_path))
    shared = tmp_path / "shared"
    cfg = make_config(tmp_path / "repo", shared_dir=shared, claude={"enabled": True})
    skills = shared / "claude" / "skills"
    stale = skills / "jailbee-usage"
    stale.mkdir(parents=True)
    (stale / "OLD.md").write_text("deleted upstream\n")
    claude_skills.sync_jailbee_skills(cfg)
    assert not (skills / "jailbee-usage" / "OLD.md").exists()
    assert (skills / "jailbee-usage" / "SKILL.md").is_file()


def test_sync_leaves_unrelated_skills_untouched(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(claude_skills, "_skills_root", lambda: _fake_skills_root(tmp_path))
    shared = tmp_path / "shared"
    cfg = make_config(tmp_path / "repo", shared_dir=shared, claude={"enabled": True})
    skills = shared / "claude" / "skills"
    other = skills / "my-own-skill"
    other.mkdir(parents=True)
    (other / "SKILL.md").write_text("mine\n")
    claude_skills.sync_jailbee_skills(cfg)
    assert (other / "SKILL.md").read_text() == "mine\n"
