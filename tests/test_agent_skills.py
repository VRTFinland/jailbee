from __future__ import annotations

from pathlib import Path

from jailbee import agent_skills
from jailbee.config import CONTAINER_USERNAME
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


def test_bundled_skills_include_issue_management() -> None:
    assert "jailbee-issue-management" in agent_skills.bundled_skill_names()


def test_issue_management_skill_routes_writes_through_host() -> None:
    text = (Path(agent_skills._skills_root()) / "jailbee-issue-management" / "SKILL.md").read_text()
    assert "gh issue create" in text
    assert "Never" in text
    assert "~/.jailbee/issue-outbox" in text
    assert "jb issue apply" in text


def _github_permission_recipes() -> list[tuple[Path, str]]:
    """Every doc/skill file that spells out a GitHub PAT permission recipe."""
    root = Path(__file__).resolve().parents[1]
    paths = [
        root / "docs" / "config.md",
        root / "docs" / "git-bridge.md",
        root / "docs" / "skills" / "jailbee-repo-setup" / "references" / "config-schema.md",
    ]
    return [(path, path.read_text()) for path in paths]


def test_github_permission_recipes_are_read_only() -> None:
    for path, text in _github_permission_recipes():
        assert "Issues: Read" in text, path
        assert "Pull requests: Read" in text, path
        assert "Read and write" not in text, path
        assert "Issues:RW" not in text, path
        assert "Pull requests:RW" not in text, path


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
    out = capsys.readouterr().out.replace("\n", "")
    assert "agents.mine:" in out
    assert "~/nowhere/skills" in out
    assert (shared / "claude" / "skills" / "jailbee-usage").is_dir()


def test_sync_prefers_the_deepest_matching_mount(tmp_path: Path, monkeypatch) -> None:
    """With nested dir mounts the narrowest one shadows the broad one
    in-container, so the skills must land under the narrow mount's subpath —
    writing under the broad mount puts them behind the shadow and the agent
    never sees them, with no warning."""
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
                "shared": [
                    {"subpath": "home", "path": "~"},
                    {"subpath": "my-agent", "path": "~/.mine"},
                ],
            }
        },
    )
    agent_skills.sync_agent_skills(cfg)
    assert (shared / "my-agent" / "skills" / "jailbee-usage" / "SKILL.md").is_file()
    assert not (shared / "home" / ".mine" / "skills").exists()


def test_sync_skips_a_mount_whose_subpath_escapes_shared_dir(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """Defense in depth: a `..` in a mount's own `subpath` spelling reaches the
    host-side join, and `_copy_skills_into` would `mkdir`/`rmtree` outside
    `<shared_dir>`. Such an agent is warned and skipped instead."""
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
                "shared": [{"subpath": "mine/../../evil", "path": "~/.mine"}],
            }
        },
    )
    agent_skills.sync_agent_skills(cfg)
    out = capsys.readouterr().out.replace("\n", "")
    assert "agents.mine:" in out
    assert "~/.mine/skills" in out
    assert not (tmp_path / "evil").exists()
    assert not (shared / "mine" / "skills").exists()


def test_sync_warning_preserves_bracketed_skills_dir(tmp_path: Path, monkeypatch, capsys) -> None:
    """`warn` runs the message through Rich markup, which eats a bracketed
    path segment; `warn_plain` keeps the warning truthful."""
    monkeypatch.setattr(agent_skills, "_skills_root", lambda: _fake_skills_root(tmp_path))
    shared = tmp_path / "shared"
    cfg = make_config(
        tmp_path / "repo",
        shared_dir=shared,
        agents={
            "mine": {
                "enabled": True,
                "command": "mine",
                "skills_dir": "~/[weird]/skills",
                "shared": [{"subpath": "mine", "path": "~/.mine"}],
            }
        },
    )
    agent_skills.sync_agent_skills(cfg)
    out = capsys.readouterr().out
    assert "~/[weird]/skills" in out


def test_sync_dedupes_two_agents_resolving_to_one_host_dir(
    tmp_path: Path, monkeypatch, mocker
) -> None:
    """Two agents may legitimately share a host directory (claude and a custom
    agent reading ~/.claude/skills). The bytes are copied once, no error, and
    a third agent with its own directory is unaffected."""
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
                "skills_dir": "~/.claude/skills",
                "shared": [{"subpath": "claude", "path": "~/.claude"}],
            },
            "codex": {"enabled": True},
        },
    )
    spy = mocker.spy(agent_skills, "_copy_skills_into")
    agent_skills.sync_agent_skills(cfg)
    calls = [call.args[0] for call in spy.call_args_list]
    assert calls.count(shared / "claude" / "skills") == 1
    assert calls.count(shared / "codex" / "skills") == 1
    assert (shared / "claude" / "skills" / "jailbee-usage" / "SKILL.md").is_file()
    assert (shared / "codex" / "skills" / "jailbee-usage" / "SKILL.md").is_file()


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

    written = agent_skills.install_host_skills(
        [tmp_path / ".claude" / "skills", tmp_path / ".codex" / "skills"]
    )

    assert (tmp_path / ".claude" / "skills" / "jailbee-usage" / "SKILL.md").is_file()
    assert (tmp_path / ".codex" / "skills" / "jailbee-usage" / "SKILL.md").is_file()
    assert len(written) == 4  # two skills, two targets


def test_install_host_skills_warns_and_continues_past_a_bad_target(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """One unwritable target must not abort the command and leave every other
    agent unwritten — warn with the target and continue."""
    monkeypatch.setattr(agent_skills, "_skills_root", lambda: _fake_skills_root(tmp_path))
    bad = tmp_path / "bad"
    bad.write_text("a file where a directory is needed\n")
    good = tmp_path / "good" / "skills"

    written = agent_skills.install_host_skills([bad, good])

    out = capsys.readouterr().out
    assert str(bad) in out
    assert (good / "jailbee-usage" / "SKILL.md").is_file()
    assert len(written) == 2  # only the good target's two skills


def test_sync_matches_a_mount_spelled_absolutely(tmp_path: Path, monkeypatch) -> None:
    """`shared[].path` and `skills_dir` both accept `~`-relative *and*
    absolute spellings, and a config may mix them. `/home/dev/.mine` covers
    `~/.mine/skills`; matching the raw strings would skip the agent."""
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
                "shared": [{"subpath": "my-agent", "path": f"/home/{CONTAINER_USERNAME}/.mine"}],
            }
        },
    )
    agent_skills.sync_agent_skills(cfg)
    assert (shared / "my-agent" / "skills" / "jailbee-usage" / "SKILL.md").is_file()


def test_sync_matches_an_absolute_skills_dir(tmp_path: Path, monkeypatch) -> None:
    """The mirror image: a `~`-relative mount covering an absolute `skills_dir`."""
    monkeypatch.setattr(agent_skills, "_skills_root", lambda: _fake_skills_root(tmp_path))
    shared = tmp_path / "shared"
    cfg = make_config(
        tmp_path / "repo",
        shared_dir=shared,
        agents={
            "mine": {
                "enabled": True,
                "command": "mine",
                "skills_dir": f"/home/{CONTAINER_USERNAME}/.mine/skills",
                "shared": [{"subpath": "my-agent", "path": "~/.mine"}],
            }
        },
    )
    agent_skills.sync_agent_skills(cfg)
    assert (shared / "my-agent" / "skills" / "jailbee-usage" / "SKILL.md").is_file()


def test_sync_prefers_the_deepest_mount_across_spellings(tmp_path: Path, monkeypatch) -> None:
    """Depth is compared on the normalised paths, so a `~` mount and an
    absolute one still order correctly against each other."""
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
                "shared": [
                    {"subpath": "my-agent", "path": f"/home/{CONTAINER_USERNAME}/.mine"},
                    {"subpath": "home", "path": "~"},
                ],
            }
        },
    )
    agent_skills.sync_agent_skills(cfg)
    assert (shared / "my-agent" / "skills" / "jailbee-usage" / "SKILL.md").is_file()
    assert not (shared / "home" / ".mine" / "skills").exists()


def test_sync_skips_a_skills_dir_hidden_by_a_private_subpath(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """`private` subpaths are mounted over with a per-container directory
    (`agent_private.attach`), so a copy underneath one is invisible to every
    agent. Warn and skip instead of writing bytes nobody can read."""
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
                "skills_dir": "~/.mine/state/skills",
                "shared": [
                    {"subpath": "my-agent", "path": "~/.mine", "private": ["state"]},
                ],
            },
        },
    )
    agent_skills.sync_agent_skills(cfg)
    out = capsys.readouterr().out.replace("\n", "")
    assert "agents.mine:" in out
    assert "~/.mine/state/skills" in out
    assert "private" in out
    assert not (shared / "my-agent" / "state" / "skills").exists()
    # The other agent is still served.
    assert (shared / "claude" / "skills" / "jailbee-usage").is_dir()


def test_sync_allows_a_private_subpath_beside_the_skills_dir(tmp_path: Path, monkeypatch) -> None:
    """Only a `private` entry that *contains* `skills_dir` hides it; a sibling
    carve-out (codex's socket directories, say) is none of its business."""
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
                "shared": [
                    {"subpath": "my-agent", "path": "~/.mine", "private": ["run"]},
                ],
            }
        },
    )
    agent_skills.sync_agent_skills(cfg)
    assert (shared / "my-agent" / "skills" / "jailbee-usage" / "SKILL.md").is_file()


def test_sync_warns_and_continues_past_an_unwritable_destination(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """One agent's destination failing must not cost every later agent its
    skills — the same guarantee `install_host_skills` gives the host side."""
    monkeypatch.setattr(agent_skills, "_skills_root", lambda: _fake_skills_root(tmp_path))
    shared = tmp_path / "shared"
    blocked = shared / "claude" / "skills"
    blocked.parent.mkdir(parents=True)
    blocked.write_text("a file where a directory is needed\n")
    cfg = make_config(
        tmp_path / "repo",
        shared_dir=shared,
        agents={"claude": {"enabled": True}, "codex": {"enabled": True}},
    )
    agent_skills.sync_agent_skills(cfg)
    # The console wraps a long tmp_path across lines; join them before matching.
    out = capsys.readouterr().out.replace("\n", "")
    assert str(blocked) in out
    assert (shared / "codex" / "skills" / "jailbee-usage" / "SKILL.md").is_file()


def test_sync_resolves_against_another_enabled_agents_mount(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """opencode reads Claude-compatible `~/.claude/skills` too, and a custom
    agent may be pointed there deliberately. That directory *is* mounted in
    the container — by claude — so the skills belong under claude's subpath,
    not in a warning claiming no mount covers it."""
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
                "skills_dir": "~/.claude/skills",
                "shared": [{"subpath": "my-agent", "path": "~/.mine"}],
            },
        },
    )
    agent_skills.sync_agent_skills(cfg)
    assert (shared / "claude" / "skills" / "jailbee-usage" / "SKILL.md").is_file()
    assert not (shared / "my-agent" / "skills").exists()
    # The warning would be false on both counts: the directory *is* mounted,
    # and the skills *are* there.
    assert "agents.mine:" not in capsys.readouterr().out.replace("\n", "")


def test_sync_ignores_a_disabled_agents_mount(tmp_path: Path, monkeypatch, capsys) -> None:
    """A disabled agent contributes no device to the profile, so its mount
    cannot carry anybody's skills — the warning is the truthful answer."""
    monkeypatch.setattr(agent_skills, "_skills_root", lambda: _fake_skills_root(tmp_path))
    shared = tmp_path / "shared"
    cfg = make_config(
        tmp_path / "repo",
        shared_dir=shared,
        agents={
            "claude": {"enabled": False},
            "mine": {
                "enabled": True,
                "command": "mine",
                "skills_dir": "~/.claude/skills",
                "shared": [{"subpath": "my-agent", "path": "~/.mine"}],
            },
        },
    )
    agent_skills.sync_agent_skills(cfg)
    assert "agents.mine:" in capsys.readouterr().out.replace("\n", "")
    assert not (shared / "claude" / "skills").exists()


def test_sync_skips_a_file_mount_covering_the_skills_dir(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """A `type: file` mount bind-mounts one file. It can never contain a
    directory, so it must not be treated as covering one."""
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
                "shared": [{"subpath": "mine-conf", "path": "~/.mine", "type": "file"}],
            }
        },
    )
    agent_skills.sync_agent_skills(cfg)
    assert "agents.mine:" in capsys.readouterr().out.replace("\n", "")
    assert not (shared / "mine-conf").exists()


def test_sync_handles_a_skills_dir_that_is_the_mount_root(tmp_path: Path, monkeypatch) -> None:
    """`skills_dir` may *be* the mount: the skills then land directly in the
    mount's own subpath, with no remainder to append."""
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
                "shared": [{"subpath": "my-skills", "path": "~/.mine/skills"}],
            }
        },
    )
    agent_skills.sync_agent_skills(cfg)
    assert (shared / "my-skills" / "jailbee-usage" / "SKILL.md").is_file()


def test_copy_survives_one_unreplaceable_skill(tmp_path: Path, monkeypatch, capsys) -> None:
    """One skill that cannot be replaced must not cost the destination its
    other skills — the same guarantee one level down from the per-destination
    guard."""
    monkeypatch.setattr(agent_skills, "_skills_root", lambda: _fake_skills_root(tmp_path))
    dest = tmp_path / "dest"
    real_copytree = agent_skills.shutil.copytree

    def copytree(src, dst, *args, **kwargs):
        if Path(src).name == "jailbee-repo-setup":
            raise PermissionError(13, "Permission denied")
        return real_copytree(src, dst, *args, **kwargs)

    monkeypatch.setattr(agent_skills.shutil, "copytree", copytree)

    written = agent_skills._copy_skills_into(dest)

    assert [p.name for p in written] == ["jailbee-usage"]
    assert (dest / "jailbee-usage" / "SKILL.md").is_file()
    assert "jailbee-repo-setup" in capsys.readouterr().out.replace("\n", "")


def test_copy_leaves_the_previous_skill_behind_when_it_fails(tmp_path: Path, monkeypatch) -> None:
    """A failed replacement must not take the working copy with it: the
    destination is what every container reads, so the old skill stays."""
    monkeypatch.setattr(agent_skills, "_skills_root", lambda: _fake_skills_root(tmp_path))
    dest = tmp_path / "dest"
    previous = dest / "jailbee-usage" / "SKILL.md"
    previous.parent.mkdir(parents=True)
    previous.write_text("the previous version\n")
    monkeypatch.setattr(
        agent_skills.shutil,
        "copytree",
        lambda *a, **k: (_ for _ in ()).throw(OSError(28, "No space left on device")),
    )

    agent_skills._copy_skills_into(dest)

    assert previous.read_text() == "the previous version\n"
    assert [p.name for p in dest.iterdir()] == ["jailbee-usage"]


def test_copy_never_shows_a_half_written_skill(tmp_path: Path, monkeypatch) -> None:
    """The destination is a live shared mount: an agent starting in another
    container mid-`apply` must see the old skill or the new one, never a
    directory with no SKILL.md. The copy therefore lands beside the target
    and is swapped in, rather than being written into place."""
    monkeypatch.setattr(agent_skills, "_skills_root", lambda: _fake_skills_root(tmp_path))
    dest = tmp_path / "dest"
    target = dest / "jailbee-usage"
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text("the previous version\n")

    seen: list[bool] = []
    real_copytree = agent_skills.shutil.copytree

    def copytree(src, dst, *args, **kwargs):
        out = real_copytree(src, dst, *args, **kwargs)
        # Mid-run, as far as any reader of `target` is concerned.
        seen.append((target / "SKILL.md").is_file())
        return out

    monkeypatch.setattr(agent_skills.shutil, "copytree", copytree)

    agent_skills._copy_skills_into(dest)

    assert seen and all(seen), "the target must stay readable while the copy runs"
    assert (target / "SKILL.md").read_text() == "usage skill\n"
    # Nothing staged is left behind for an agent to scan.
    assert sorted(p.name for p in dest.iterdir()) == ["jailbee-repo-setup", "jailbee-usage"]


# These two run against the *real* bundled skills rather than the synthetic
# tree above, which is what keeps `_skills_root`'s packaged/dev fallback and
# the replacement semantics honest against what actually ships.


def test_install_host_skills_replaces_a_stale_copy(tmp_path: Path) -> None:
    """Files removed upstream must disappear, as `make install-skill` did."""
    stale = tmp_path / "skills" / "jailbee-usage" / "GONE.md"
    stale.parent.mkdir(parents=True)
    stale.write_text("removed upstream")

    agent_skills.install_host_skills([tmp_path / "skills"])

    assert not stale.exists()
    assert (stale.parent / "SKILL.md").is_file()


def test_install_host_skills_leaves_unrelated_skills_alone(tmp_path: Path) -> None:
    mine = tmp_path / "skills" / "my-own-skill" / "SKILL.md"
    mine.parent.mkdir(parents=True)
    mine.write_text("mine")

    agent_skills.install_host_skills([tmp_path / "skills"])

    assert mine.read_text() == "mine"
