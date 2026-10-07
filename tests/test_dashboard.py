"""Tests for the gie dashboard module (pure logic; no real Incus/TTY)."""

from __future__ import annotations

import contextlib
import dataclasses
import io
import itertools
import json
import os
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from rich.console import Console, RenderableType

from jailbee import dashboard
from jailbee import dashboard_details as dd
from jailbee.accounts.models import AgentActivity
from jailbee.agent_status import AgentSummary
from jailbee.config.loader import _scratch_prefix
from jailbee.dashboard_jobs import JobResult, JobRunner
from jailbee.egress_scope import EntryRow
from jailbee.git_status import GitStatus
from jailbee.lifecycle import ContainerInfo
from jailbee.state_service.protocol import Snapshot


def test_inline_editor_keeps_shortcuts_as_text():
    state = dashboard.CommandState(text="", suggestions=(), index=0)
    assert dashboard.edit_command(state, b"q").text == "q"


def test_inline_editor_handles_editing_and_utf8():
    state = dashboard.CommandState(text="", suggestions=(), index=0)
    state = dashboard.edit_command(state, "é shell".encode())
    state = dashboard.edit_command(state, b"\x7f")
    assert state.text == "é shel"


def test_inline_editor_preserves_utf8_split_across_scripted_reads(mocker):
    rendered = mocker.patch.object(dashboard, "render", wraps=dashboard.render)

    _drive_run(mocker, [b"!", b"\xc3", b"\xa9", b"\x1b", b"q"])

    assert any(
        isinstance(call.kwargs.get("overlay"), dashboard.CommandState)
        and call.kwargs["overlay"].text == "é"
        for call in rendered.call_args_list
    )


def test_inline_editor_backspace_clears_pending_utf8_before_completed_text():
    state = dashboard.CommandState(text="a")
    state = dashboard.edit_command(state, b"\xc3")
    assert state.pending_utf8 == b"\xc3"

    state = dashboard.edit_command(state, b"\x7f")

    assert state.text == "a"
    assert state.pending_utf8 == b""


def test_inline_editor_tab_cycles_candidates():
    state = dashboard.CommandState(text="me", suggestions=("merge", "menu"), index=0)
    state = dashboard.edit_command(state, b"\t")
    assert state.text == "menu"
    assert state.index == 1


def test_inline_editor_tab_without_candidates_is_safe():
    state = dashboard.CommandState(text="merge '", suggestions=())
    assert dashboard.edit_command(state, b"\t") == state


def test_inline_editor_completion_preserves_unfinished_quote():
    from jailbee.dashboard_commands import completion_candidates

    text = "shell 'feature"
    state = dashboard.CommandState(
        text=text, suggestions=completion_candidates(text, ("feature branch",))
    )
    assert "feature branch" in state.suggestions


def test_command_binding_and_inline_render_keep_table_visible():
    group = dashboard.RepoGroup("alpha", "/alpha", None, [_ci("alpha-x", "alpha")])
    overlay = dashboard.CommandState(text="git d", suggestions=("git diff",))
    screen = Console(width=100, record=True)
    screen.print(
        dashboard.render(
            [group],
            dashboard.Row("container", "alpha-x"),
            now=datetime.now(UTC),
            git_enabled=True,
            overlay=overlay,
        )
    )
    rendered = screen.export_text()
    assert dashboard.parse_key(b"!") == "command"
    assert "  x" in rendered
    assert "git d" in rendered
    assert "git diff" in rendered


def test_render_title_has_no_refresh_clock():
    group = dashboard.RepoGroup("alpha", "/alpha", None, [_ci("alpha-x", "alpha")])
    frame = _render_text(
        dashboard.render(
            [group], None, now=datetime(2026, 6, 8, 12, 0, 5, tzinfo=UTC), git_enabled=True
        )
    )
    assert "12:00:05" in frame
    assert "↻" not in frame
    assert "s/" not in frame.splitlines()[0]


def test_nothing_to_show_message_blames_no_single_cause():
    """The launch guard fires whenever the cwd resolves to no repo, and that
    has several causes: no config file with `scratch.enabled` false, but also
    `$HOME` or the filesystem root (both refused), a directory name that
    slugifies to nothing, and a config file that exists but will not parse.
    Naming one of them would misdiagnose the other four.
    """
    assert "scratch" not in dashboard.NOTHING_TO_SHOW


def test_nothing_to_show_message_still_offers_a_way_out():
    """Naming no cause must not also mean naming no remedy: the old wording
    carried the next step by implication, and dropping it left the user with a
    dead end. The remedy stays cause-neutral — both branches out of it work
    whichever of the causes fired."""
    assert "jailbee config init" in dashboard.NOTHING_TO_SHOW
    assert "registered repo" in dashboard.NOTHING_TO_SHOW


def test_nothing_to_show_message_also_points_at_config_validate():
    """One of the causes is a config file that exists but will not parse —
    `jailbee config init` is the wrong remedy for that (it errors rather than
    overwriting), so the message must also point at `jailbee config validate`.
    """
    assert "jailbee config validate" in dashboard.NOTHING_TO_SHOW


def test_collect_repo_roots_puts_cwd_first_and_dedupes(mocker):
    a = Path("/repos/a")
    b = Path("/repos/b")
    mocker.patch.object(dashboard, "registered_repo_roots", return_value=[a, b])
    # cwd root equals an already-registered one -> no duplicate, cwd wins order
    result = dashboard.collect_repo_roots(b)
    assert result == [b, a]


def test_collect_repo_roots_no_cwd(mocker):
    a = Path("/repos/a")
    mocker.patch.object(dashboard, "registered_repo_roots", return_value=[a])
    assert dashboard.collect_repo_roots(None) == [a]


def test_collect_repo_roots_empty(mocker):
    mocker.patch.object(dashboard, "registered_repo_roots", return_value=[])
    assert dashboard.collect_repo_roots(None) == []


def test_registered_repo_roots_skips_a_missing_directory(db_session, tmp_path, mocker):
    """The registry is keyed on the repo root, and a root with no config file
    is still a real repo (the scratch case) — only a vanished directory is
    skipped."""
    from jailbee.db.models import RegisteredRepo

    # `live` deliberately has NO .jailbee/config.yaml: it must still be listed.
    live = tmp_path / "live"
    live.mkdir()

    db_session.add(
        RegisteredRepo(
            container_prefix="live",
            repo_root=str(live),
            registered_at=datetime.now(UTC),
        )
    )
    db_session.add(
        RegisteredRepo(
            container_prefix="gone",
            repo_root=str(tmp_path / "nonexistent"),
            registered_at=datetime.now(UTC),
        )
    )
    db_session.commit()

    mocker.patch(
        "jailbee.db.get_engine",
        return_value=db_session.get_bind(),
    )
    assert dashboard.registered_repo_roots() == [live]


def test_registered_repo_roots_filters_excluded_prefix_before_loading(db_session, tmp_path, mocker):
    from jailbee.db.models import RegisteredRepo
    from jailbee.remote_ssh.repo_scope import RemoteRepoScope

    roots = [tmp_path / "allowed", tmp_path / "secret"]
    for root in roots:
        root.mkdir()
        db_session.add(
            RegisteredRepo(
                container_prefix=root.name,
                repo_root=str(root),
                registered_at=datetime.now(UTC),
            )
        )
    db_session.commit()
    mocker.patch("jailbee.db.get_engine", return_value=db_session.get_bind())

    scope = RemoteRepoScope(frozenset({"secret"}))
    assert dashboard.registered_repo_roots(scope=scope) == [roots[0]]


def _ci(
    name: str,
    repo: str,
    state: str = "Running",
    *,
    mode: str = "clone",
    pr_number: int | None = None,
    job_phase: str | None = None,
    job_pid: int | None = None,
    git_status: GitStatus | None = None,
) -> ContainerInfo:
    return ContainerInfo(
        name=name,
        state=state,
        network="strict",
        ip=None,
        memory_limit=None,
        repo=repo,
        mode=mode,
        pr_number=pr_number,
        job_phase=job_phase,
        job_pid=job_pid,
        git_status=git_status,
    )


def _repo_dir(tmp_path: Path, name: str) -> Path:
    """A repo root with a real config file on disk, returning the root.

    ``gather_rows`` reads ``repo_config_path(root)`` off the filesystem to fill
    ``RepoGroup.config_path``, so a test asserting on that path needs the file
    to exist. Its contents never matter — ``load_repo_config`` is mocked in
    these tests.
    """
    root = tmp_path / name
    (root / ".jailbee").mkdir(parents=True)
    (root / ".jailbee" / "config.yaml").write_text("{}\n")
    return root


def _ctx(**kw: object) -> dashboard.MenuContext:
    """A running clone container in a repo whose config loaded.

    The defaults are the common case; each test overrides only the field its
    subject is about.
    """
    fields: dict[str, object] = {
        "state": "Running",
        "has_repo": True,
        "current_network": "strict",
    }
    fields.update(kw)
    return dashboard.MenuContext(**fields)


def _apps(*verbs: str) -> list[dashboard.AppMenuEntry]:
    """``AppMenuEntry`` list where each label defaults to its own verb — the
    description-less fallback most tests don't care to distinguish from."""
    return [dashboard.AppMenuEntry(v, v) for v in verbs]


def _dirty(**kw: str) -> GitStatus:
    """A GitStatus with committed work and a dirty tree unless overridden."""
    fields: dict[str, str] = {
        "wt": "+12 -3",
        "ahead_diff": "+245 -18",
        "ahead_count": "3",
        "conflict": "ok",
    }
    fields.update(kw)
    return GitStatus(**fields)


def test_gather_rows_sorts_named_repos_alphabetically(tmp_path, mocker, make_cfg):
    cwd_root = _repo_dir(tmp_path, "beta")  # container_prefix == "beta"
    other_root = _repo_dir(tmp_path, "alpha")  # container_prefix == "alpha"
    cwd_cfg = make_cfg(cwd_root)
    other_cfg = make_cfg(other_root)

    def fake_load(root):
        return cwd_cfg if root == cwd_root else other_cfg

    def fake_list(cfg, incus, *, all_repos, with_git_status, with_background, instances):
        if all_repos:
            return []  # no orphans
        if cfg is cwd_cfg:
            return [_ci("beta-one", "beta")]
        return [_ci("alpha-one", "alpha")]

    mocker.patch.object(dashboard, "load_repo_config", side_effect=fake_load)
    mocker.patch.object(dashboard, "list_containers", side_effect=fake_list)

    groups = dashboard.gather_rows(mocker.MagicMock(), [other_root, cwd_root], with_git=False)
    assert [g.prefix for g in groups] == ["alpha", "beta"]
    assert groups[1].config_path == cwd_root / ".jailbee" / "config.yaml"
    assert [c.name for c in groups[1].containers] == ["beta-one"]


def test_gather_rows_includes_a_repo_with_no_config_file(tmp_path, monkeypatch, mocker):
    """A scratch repo is a repo: it must group its own containers rather than
    fall through to the view-only orphan bucket.

    Uses the real `load_repo_config`, so this exercises the synthesis path end
    to end; only git probing and the container listing are mocked.
    """
    xdg = tmp_path / ".config"
    (xdg / "jailbee").mkdir(parents=True)
    (xdg / "jailbee" / "global.yaml").write_text("{}\n")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    mocker.patch("jailbee.config.loader.detect_default_branch", return_value="main")
    mocker.patch("jailbee.config.loader.detect_upstream_remote", return_value="origin")
    repo = tmp_path / "tutkimus"
    (repo / ".git").mkdir(parents=True)
    prefix = _scratch_prefix(repo)  # slug plus a digest of the path

    mocker.patch.object(dashboard, "list_containers", return_value=[])

    groups = dashboard.gather_rows(mocker.MagicMock(), [repo], with_git=False)

    assert [g.prefix for g in groups] == [prefix]
    assert groups[0].repo_root == str(repo)
    # No file on disk -> no config path. Task 10b is what re-enables its menu.
    assert groups[0].config_path is None
    assert groups[0].containers == []
    target = dashboard.RepoTarget.of(groups[0])
    assert target is not None
    assert target.repo_root == repo


def test_gather_rows_carries_the_repos_loose_ttl_default(tmp_path, mocker, make_cfg):
    """The Qt duration dialog pre-selects this, so it must be the repo's own
    configured `loose_auto_revert.after`, not the first preset."""
    cfg = make_cfg(tmp_path / "alpha", loose_auto_revert={"after": "45m"})
    root = tmp_path / "alpha"
    mocker.patch.object(dashboard, "load_repo_config", return_value=cfg)

    def fake_list(c, incus, *, all_repos, with_git_status, with_background, instances):
        return [] if all_repos else [_ci("alpha-one", "alpha")]

    mocker.patch.object(dashboard, "list_containers", side_effect=fake_list)

    groups = dashboard.gather_rows(mocker.MagicMock(), [root], with_git=False)

    assert groups[0].loose_ttl_default == "45m"


def test_gather_rows_carries_the_repos_optional_mount_kinds(tmp_path, mocker, make_cfg):
    cfg = make_cfg(
        tmp_path / "alpha",
        optional_mounts={
            "aws": {"host": str(tmp_path), "container": "/home/dev/.aws"},
            "gcloud": {"host": str(tmp_path), "container": "/home/dev/.config/gcloud"},
        },
    )
    root = tmp_path / "alpha"
    mocker.patch.object(dashboard, "load_repo_config", return_value=cfg)

    def fake_list(c, incus, *, all_repos, with_git_status, with_background, instances):
        return [] if all_repos else [_ci("alpha-one", "alpha")]

    mocker.patch.object(dashboard, "list_containers", side_effect=fake_list)

    groups = dashboard.gather_rows(mocker.MagicMock(), [root], with_git=False)

    assert groups[0].optional_mounts == ("aws", "gcloud")


def test_orphan_groups_have_no_optional_mounts():
    assert dashboard.RepoGroup("orphan", None, None, []).optional_mounts == ()


def test_gather_rows_loose_ttl_default_is_none_when_policy_disabled(tmp_path, mocker, make_cfg):
    """None tells the GUI not to ask: a disabled policy schedules no TTL."""
    cfg = make_cfg(tmp_path / "alpha", loose_auto_revert={"enabled": False})
    root = tmp_path / "alpha"
    mocker.patch.object(dashboard, "load_repo_config", return_value=cfg)

    def fake_list(c, incus, *, all_repos, with_git_status, with_background, instances):
        return [] if all_repos else [_ci("alpha-one", "alpha")]

    mocker.patch.object(dashboard, "list_containers", side_effect=fake_list)

    groups = dashboard.gather_rows(mocker.MagicMock(), [root], with_git=False)

    assert groups[0].loose_ttl_default is None


def test_gather_rows_carries_the_repos_agent_homes(tmp_path, mocker, make_cfg):
    from tests.conftest import with_agent

    cfg = with_agent(
        make_cfg(tmp_path / "alpha", shared_dir=tmp_path / "shared"), "claude", enabled=True
    )
    root = tmp_path / "alpha"
    mocker.patch.object(dashboard, "load_repo_config", return_value=cfg)

    def fake_list(c, incus, *, all_repos, with_git_status, with_background, instances):
        return [_ci("orphan-x", "orphan")] if all_repos else [_ci("alpha-one", "alpha")]

    mocker.patch.object(dashboard, "list_containers", side_effect=fake_list)

    groups = dashboard.gather_rows(mocker.MagicMock(), [root], with_git=False)

    by_prefix = {g.prefix: g for g in groups}
    assert by_prefix[cfg.container_prefix].agent_homes == (
        ("alpha-one", "claude", tmp_path / "shared" / ".private" / "alpha-one" / "claude"),
    )
    assert by_prefix["orphan"].agent_homes == ()


def test_gather_rows_records_the_repos_push_defaults(tmp_path, mocker, make_cfg):
    """The Qt dashboard asks the merge/rebase question itself, and only when
    the repo left it unanswered — so the group has to carry the answer."""
    cfg = make_cfg(
        tmp_path / "alpha",
        push={"default_action": "rebase", "default_source": "current"},
    )
    root = tmp_path / "alpha"
    mocker.patch.object(dashboard, "load_repo_config", return_value=cfg)

    def fake_list(c, incus, *, all_repos, with_git_status, with_background, instances):
        return [] if all_repos else [_ci("alpha-one", "alpha")]

    mocker.patch.object(dashboard, "list_containers", side_effect=fake_list)

    groups = dashboard.gather_rows(mocker.MagicMock(), [root], with_git=False)

    assert groups[0].push_action_default == "rebase"
    assert groups[0].push_source_default == "current"


def test_gather_rows_push_defaults_fall_back_to_the_config_defaults(tmp_path, mocker, make_cfg):
    """PushConfig's own defaults: 'ask' is why the GUI has a dialog at all."""
    cfg = make_cfg(tmp_path / "alpha")
    root = tmp_path / "alpha"
    mocker.patch.object(dashboard, "load_repo_config", return_value=cfg)

    def fake_list(c, incus, *, all_repos, with_git_status, with_background, instances):
        return [] if all_repos else [_ci("alpha-one", "alpha")]

    mocker.patch.object(dashboard, "list_containers", side_effect=fake_list)

    groups = dashboard.gather_rows(mocker.MagicMock(), [root], with_git=False)

    assert groups[0].push_action_default == "ask"
    assert groups[0].push_source_default == "base"


def test_gather_rows_renders_an_int_after_as_minutes(tmp_path, mocker, make_cfg):
    cfg = make_cfg(tmp_path / "alpha", loose_auto_revert={"after": 20})
    root = tmp_path / "alpha"
    mocker.patch.object(dashboard, "load_repo_config", return_value=cfg)

    def fake_list(c, incus, *, all_repos, with_git_status, with_background, instances):
        return [] if all_repos else [_ci("alpha-one", "alpha")]

    mocker.patch.object(dashboard, "list_containers", side_effect=fake_list)

    groups = dashboard.gather_rows(mocker.MagicMock(), [root], with_git=False)

    assert groups[0].loose_ttl_default == "20m"


def test_gather_rows_orphan_group_has_no_loose_ttl_default(tmp_path, mocker, make_cfg):
    cfg = make_cfg(tmp_path / "alpha")
    root = tmp_path / "alpha"
    mocker.patch.object(dashboard, "load_repo_config", return_value=cfg)

    def fake_list(c, incus, *, all_repos, with_git_status, with_background, instances):
        if all_repos:
            return [_ci("alpha-one", "alpha"), _ci("gamma-x", "gamma")]
        return [_ci("alpha-one", "alpha")]

    mocker.patch.object(dashboard, "list_containers", side_effect=fake_list)

    groups = dashboard.gather_rows(mocker.MagicMock(), [root], with_git=False)

    orphan = next(g for g in groups if g.prefix == "gamma")
    assert orphan.loose_ttl_default is None


def test_gather_rows_surfaces_orphans_view_only(tmp_path, mocker, make_cfg):
    cfg = make_cfg(tmp_path / "alpha")
    root = tmp_path / "alpha"
    mocker.patch.object(dashboard, "load_repo_config", return_value=cfg)

    def fake_list(c, incus, *, all_repos, with_git_status, with_background, instances):
        if all_repos:
            return [_ci("alpha-one", "alpha"), _ci("gamma-x", "gamma")]
        return [_ci("alpha-one", "alpha")]

    mocker.patch.object(dashboard, "list_containers", side_effect=fake_list)
    groups = dashboard.gather_rows(mocker.MagicMock(), [root], with_git=False)
    orphan = next(g for g in groups if g.prefix == "gamma")
    assert orphan.config_path is None
    assert orphan.repo_root is None
    assert [c.name for c in orphan.containers] == ["gamma-x"]
    # no container appears twice
    names = [c.name for g in groups for c in g.containers]
    assert sorted(names) == ["alpha-one", "gamma-x"]


def test_gather_rows_cwd_none_orphans_sort_last(tmp_path, mocker, make_cfg):
    # `beta` has a config file, `alpha` does not (the scratch case). Only the
    # container-less `zeta` is an orphan, so a discriminator that keyed on the
    # missing config file would sink `alpha` into the orphan tier as well.
    beta_root = _repo_dir(tmp_path, "beta")
    alpha_root = tmp_path / "alpha"
    beta = make_cfg(beta_root)
    alpha = make_cfg(alpha_root)

    def fake_load(root):
        return alpha if root == alpha_root else beta

    def fake_list(cfg, incus, *, all_repos, with_git_status, with_background, instances):
        if all_repos:
            # one orphan ('zeta') plus the two covered repos
            return [_ci("alpha-1", "alpha"), _ci("beta-1", "beta"), _ci("zeta-x", "zeta")]
        return [_ci(f"{cfg.container_prefix}-1", cfg.container_prefix)]

    mocker.patch.object(dashboard, "load_repo_config", side_effect=fake_load)
    mocker.patch.object(dashboard, "list_containers", side_effect=fake_list)

    groups = dashboard.gather_rows(mocker.MagicMock(), [beta_root, alpha_root], with_git=False)
    # named repos alpha-sorted first, orphan group ('zeta') last
    assert [g.prefix for g in groups] == ["alpha", "beta", "zeta"]
    # A missing repo root — not a missing config file — is what makes a group
    # an orphan now, and it is what sorts it last.
    assert groups[-1].repo_root is None
    assert groups[-1].config_path is None


def test_gather_rows_includes_empty_repo_for_targeting(tmp_path, mocker, make_cfg):
    empty_root = tmp_path / "alpha"
    populated_root = tmp_path / "beta"
    empty_cfg = make_cfg(empty_root)  # container_prefix == "alpha"
    populated_cfg = make_cfg(populated_root)  # container_prefix == "beta"

    def fake_load(root):
        return empty_cfg if root == empty_root else populated_cfg

    def fake_list(cfg, incus, *, all_repos, with_git_status, with_background, instances):
        if all_repos:
            return []  # no orphans
        if cfg is empty_cfg:
            return []
        return [_ci("beta-one", "beta")]

    mocker.patch.object(dashboard, "load_repo_config", side_effect=fake_load)
    mocker.patch.object(dashboard, "list_containers", side_effect=fake_list)

    groups = dashboard.gather_rows(mocker.MagicMock(), [empty_root, populated_root], with_git=False)
    assert [g.prefix for g in groups] == ["alpha", "beta"]
    alpha = groups[0]
    assert alpha.containers == []
    assert dashboard.RepoTarget.of(alpha) is not None


def test_gather_rows_empty_repo_roots_returns_empty(mocker):
    # No repos -> no base_cfg -> no orphan scan -> empty result, no calls.
    lc = mocker.patch.object(dashboard, "list_containers")
    incus = mocker.MagicMock()
    result = dashboard.gather_rows(incus, [], with_git=False)
    assert result == []
    lc.assert_not_called()
    incus.list_containers.assert_not_called()


def test_gather_rows_lists_incus_once_for_every_repo_and_the_orphan_scan(
    tmp_path, mocker, make_cfg
):
    """Each `incus list` makes the daemon build every instance's full state,
    and the dashboards gather every few seconds: one listing per repo kept
    incusd busy for as long as any dashboard was open."""
    alpha_root, beta_root = tmp_path / "alpha", tmp_path / "beta"
    cfgs = {alpha_root: make_cfg(alpha_root), beta_root: make_cfg(beta_root)}
    mocker.patch.object(dashboard, "load_repo_config", side_effect=cfgs.__getitem__)
    lc = mocker.patch.object(dashboard, "list_containers", return_value=[])
    incus = mocker.MagicMock()

    dashboard.gather_rows(incus, [alpha_root, beta_root], with_git=False)

    incus.list_containers.assert_called_once_with()
    assert lc.call_count == 3  # two repos and the orphan scan
    assert all(
        call.kwargs["instances"] is incus.list_containers.return_value for call in lc.call_args_list
    )


def test_gather_rows_skips_unloadable_config_never_raises(tmp_path, mocker, make_cfg):
    good_root = tmp_path / "alpha"
    bad_root = tmp_path / "broken"
    good = make_cfg(good_root)

    def fake_load(root):
        if root == bad_root:
            raise OSError("gone")
        return good

    def fake_list(c, incus, *, all_repos, with_git_status, with_background, instances):
        return [] if all_repos else [_ci("alpha-one", "alpha")]

    mocker.patch.object(dashboard, "load_repo_config", side_effect=fake_load)
    mocker.patch.object(dashboard, "list_containers", side_effect=fake_list)
    groups = dashboard.gather_rows(mocker.MagicMock(), [good_root, bad_root], with_git=False)
    assert [g.prefix for g in groups] == ["alpha"]


def test_view_only_note_explains_an_orphan_group():
    groups = [dashboard.RepoGroup("gamma", None, None, [_ci("gamma-x", "gamma")])]
    note = dashboard.view_only_note(groups, "gamma-x")
    assert note is not None
    assert "gamma" in note and "view-only" in note


def test_view_only_note_is_none_for_a_scratch_group():
    """A repo with no config file is actionable — its config was synthesized,
    not missing — so there is nothing to explain and nothing to disable."""
    groups = [dashboard.RepoGroup("gamma", "/gamma", None, [_ci("gamma-x", "gamma")])]

    assert dashboard.view_only_note(groups, "gamma-x") is None


def test_actions_for_container_offers_actions_to_a_scratch_group():
    """The action menu is gated on the repo being addressable, not on a config
    file existing: a scratch repo gets the same menu as a configured one."""
    groups = [dashboard.RepoGroup("gamma", "/gamma", None, [_ci("gamma-x", "gamma")])]

    verbs = [verb for _, verb in dashboard.actions_for_container(groups, "gamma-x")]

    assert "tmux" in verbs and "destroy" in verbs


def test_remote_actions_filter_against_canonical_policy_and_argv():
    from jailbee.config.models_remote import RemoteCommandPolicy, RemoteSSHConfig
    from jailbee.dashboard_commands import dashboard_action_argv

    groups = [dashboard.RepoGroup("alpha", "/alpha", None, [_ci("alpha-1", "alpha")])]
    policy = RemoteSSHConfig(commands=RemoteCommandPolicy(mode="allowlist", allow=["git merge"]))

    assert [
        verb
        for _, verb in dashboard.actions_for_container(
            groups, "alpha-1", over_ssh=True, ssh_policy=policy
        )
    ] == ["merge"]
    assert dashboard.group_menu_actions(
        dashboard.actions_for_container(groups, "alpha-1", over_ssh=True, ssh_policy=policy)
    ) == [dashboard.MenuGroup("Git →", (("Merge into…", "merge"),))]
    assert dashboard_action_argv("tmux", "alpha-1", force=True) == ["tmux", "alpha-1", "--force"]
    assert "--config" not in dashboard_action_argv("git push --pr", "alpha-1")


def test_remote_policy_filters_all_git_leaves_without_hiding_other_actions():
    from jailbee.config.models_remote import RemoteCommandPolicy, RemoteSSHConfig

    groups = [dashboard.RepoGroup("alpha", "/alpha", None, [_ci("alpha-1", "alpha")])]
    policy = RemoteSSHConfig(commands=RemoteCommandPolicy(mode="allowlist", allow=["shell"]))

    permitted = dashboard.actions_for_container(groups, "alpha-1", over_ssh=True, ssh_policy=policy)

    assert permitted == [("Open shell", "shell")]
    assert dashboard.group_menu_actions(permitted) == [("Open shell", "shell")]


def test_remote_full_and_disabled_actions_follow_policy():
    from jailbee.config.models_remote import RemoteCommandPolicy, RemoteSSHConfig

    groups = [dashboard.RepoGroup("alpha", "/alpha", None, [_ci("alpha-1", "alpha")])]
    full = RemoteSSHConfig(commands=RemoteCommandPolicy(mode="full"))
    disabled = RemoteSSHConfig(commands=RemoteCommandPolicy(mode="disabled"))

    full_verbs = [
        verb
        for _, verb in dashboard.actions_for_container(
            groups, "alpha-1", over_ssh=True, ssh_policy=full
        )
    ]
    assert {"merge", "shell", "tmux"} <= set(full_verbs)
    assert (
        dashboard.actions_for_container(groups, "alpha-1", over_ssh=True, ssh_policy=disabled) == []
    )
    assert (
        dashboard.group_menu_actions(
            dashboard.actions_for_container(groups, "alpha-1", over_ssh=True, ssh_policy=disabled)
        )
        == []
    )


def test_view_only_note_is_none_when_the_container_has_actions():
    groups = [
        dashboard.RepoGroup(
            "alpha", "/alpha", Path("/alpha/.jailbee/config.yaml"), [_ci("alpha-1", "alpha")]
        )
    ]
    assert dashboard.view_only_note(groups, "alpha-1") is None


def test_view_only_note_is_none_for_an_unknown_container():
    """Nothing to explain about a container that is not on screen — the
    caller must stay silent rather than pop up an empty menu."""
    assert dashboard.view_only_note([], "ghost") is None


def test_gather_live_reresolves_repo_roots_on_every_gather(mocker):
    """A repo registered while a dashboard is running must be picked up by the
    next gather.

    `jailbee new` in a not-yet-registered repo registers it mid-session
    (cli.py), and the 60s pool timer can unregister/re-register a repo whose
    config file momentarily vanishes (egress_pool.refresh_all). Until the
    dashboard loads that repo's config, `gather_rows` files its containers
    under a view-only orphan group, so `actions_for_container` returns [] and
    the right-click menu silently never opens.
    """
    a = Path("/repos/a")
    b = Path("/repos/b")
    registered = [a]
    mocker.patch.object(dashboard, "registered_repo_roots", side_effect=lambda: list(registered))
    gr = mocker.patch.object(dashboard, "gather_rows", return_value=[])
    incus = mocker.MagicMock()

    dashboard.gather_live(incus, [], with_git=False)
    assert gr.call_args.args[1] == [a]

    registered.append(b)  # a `jailbee new` in repo b just registered it
    dashboard.gather_live(incus, [], with_git=True)
    assert gr.call_args.args[1] == [a, b]
    assert gr.call_args.kwargs == {"with_git": True}


def test_carry_forward_git_status_fills_in_from_previous_snapshot():
    from jailbee.git_status import GitStatus

    status = GitStatus(wt="+1 -0", ahead_diff="clean", ahead_count="1", conflict="ok")
    prev = [
        dashboard.RepoGroup(
            "a",
            "/a",
            Path("/a/.jailbee/config.yaml"),
            [_ci("a-1", "a")],
        )
    ]
    prev[0].containers[0].git_status = status

    new = [
        dashboard.RepoGroup(
            "a",
            "/a",
            Path("/a/.jailbee/config.yaml"),
            [_ci("a-1", "a")],
        )
    ]
    assert new[0].containers[0].git_status is None

    dashboard.carry_forward_git_status(new, prev)

    assert new[0].containers[0].git_status is status


def test_carry_forward_git_status_leaves_unmatched_name_none():
    from jailbee.git_status import GitStatus

    status = GitStatus(wt="+1 -0", ahead_diff="clean", ahead_count="1", conflict="ok")
    prev = [dashboard.RepoGroup("a", "/a", Path("/a/.jailbee/config.yaml"), [_ci("a-1", "a")])]
    prev[0].containers[0].git_status = status

    new = [dashboard.RepoGroup("a", "/a", Path("/a/.jailbee/config.yaml"), [_ci("a-2", "a")])]

    dashboard.carry_forward_git_status(new, prev)

    assert new[0].containers[0].git_status is None


def test_carry_forward_git_status_does_not_overwrite_existing():
    from jailbee.git_status import GitStatus

    old_status = GitStatus(wt="+1 -0", ahead_diff="clean", ahead_count="1", conflict="ok")
    new_status = GitStatus(wt="+2 -0", ahead_diff="clean", ahead_count="2", conflict="ok")
    prev = [dashboard.RepoGroup("a", "/a", Path("/a/.jailbee/config.yaml"), [_ci("a-1", "a")])]
    prev[0].containers[0].git_status = old_status

    new = [dashboard.RepoGroup("a", "/a", Path("/a/.jailbee/config.yaml"), [_ci("a-1", "a")])]
    new[0].containers[0].git_status = new_status

    dashboard.carry_forward_git_status(new, prev)

    assert new[0].containers[0].git_status is new_status


def test_carry_forward_git_status_empty_prev_is_noop():
    new = [dashboard.RepoGroup("a", "/a", Path("/a/.jailbee/config.yaml"), [_ci("a-1", "a")])]

    dashboard.carry_forward_git_status(new, [])

    assert new[0].containers[0].git_status is None


def test_selectable_rows_interleaves_headers_and_containers():
    """Repo headers are selectable rows. That is what lets `Enter` reach a
    group whose containers are hidden — and it makes the cursor behave like
    the tree it is drawing."""
    groups = [
        dashboard.RepoGroup("a", "/a", None, [_ci("a-1", "a"), _ci("a-2", "a")]),
        dashboard.RepoGroup("b", "/b", None, [_ci("b-1", "b")]),
    ]
    rows = dashboard.selectable_rows(groups)
    assert rows == [
        dashboard.Row("repo", "a"),
        dashboard.Row("container", "a-1"),
        dashboard.Row("container", "a-2"),
        dashboard.Row("repo", "b"),
        dashboard.Row("container", "b-1"),
    ]


def test_selectable_rows_skips_a_folded_groups_containers():
    """A folded group keeps its header — that is how you unfold it — and
    contributes none of its containers. Its neighbours are untouched."""
    groups = [
        dashboard.RepoGroup("a", "/a", None, [_ci("a-1", "a")]),
        dashboard.RepoGroup("b", "/b", None, [_ci("b-1", "b")]),
    ]
    assert dashboard.selectable_rows(groups, frozenset({"a"})) == [
        dashboard.Row("repo", "a"),
        dashboard.Row("repo", "b"),
        dashboard.Row("container", "b-1"),
    ]


def test_selectable_rows_includes_an_empty_group():
    groups = [dashboard.RepoGroup("a", "/a", None, [])]
    assert dashboard.selectable_rows(groups) == [dashboard.Row("repo", "a")]
    assert dashboard.new_container_target(groups, dashboard.Row("repo", "a")) is groups[0]


def test_move_selection_clamps_at_edges():
    rows = [dashboard.Row("repo", "x"), dashboard.Row("container", "x-1")]
    assert dashboard.move_selection(rows, None, 1) == rows[0]
    assert dashboard.move_selection(rows, rows[0], -1) == rows[0]  # clamp at top
    assert dashboard.move_selection(rows, rows[1], 1) == rows[1]  # clamp at bottom
    assert dashboard.move_selection(rows, rows[0], 1) == rows[1]
    assert dashboard.move_selection([], rows[0], 1) is None


def test_reconcile_selection_keeps_or_clamps():
    a, b = dashboard.Row("container", "a"), dashboard.Row("container", "b")
    assert dashboard.reconcile_selection([a, b], b, 0) == b
    assert dashboard.reconcile_selection([a], b, 1) == a
    assert dashboard.reconcile_selection([], b, 0) is None
    assert dashboard.reconcile_selection([a, b], None, 0) == a


def test_container_of_narrows_a_header_row_to_none():
    """The action path takes a container name. A header row has none, so it
    falls into the existing 'nothing selected' notice rather than needing new
    gating at every call site."""
    assert dashboard.container_of(dashboard.Row("container", "a-1")) == "a-1"
    assert dashboard.container_of(dashboard.Row("repo", "a")) is None
    assert dashboard.container_of(None) is None


def _session_verbs(actions: list[tuple[str, str]]) -> list[str]:
    """Session, app, network and lifecycle verbs, omitting workflow leaves."""
    return [
        verb
        for label, verb in actions
        if verb in {"tmux", "shell", "restart", "stop", "destroy"}
        or verb.startswith("net ")
        or label.startswith("Launch ")
    ]


def test_menu_actions_running_default_hides_ide_and_chrome():
    actions = dashboard.menu_actions(_ctx())
    assert _session_verbs(actions) == [
        "tmux",
        "shell",
        "net loose",
        "net egress ls",
        "restart",
        "stop",
        "destroy",
    ]
    verbs = [a for _, a in actions]
    assert "ide" not in verbs
    assert "chrome" not in verbs


def test_merge_is_only_offered_for_eligible_source():
    eligible = dashboard.MenuContext(state="Running", has_repo=True, mode="clone")
    stopped = dashboard.MenuContext(state="Stopped", has_repo=True, mode="clone")
    mounted = dashboard.MenuContext(state="Running", has_repo=True, mode="mount")
    orphan = dashboard.MenuContext(state="Running", has_repo=False, mode="clone")
    assert ("Merge into…", "merge") in dashboard.menu_actions(eligible)
    for context in (stopped, mounted, orphan):
        assert "merge" not in [verb for _, verb in dashboard.menu_actions(context)]


def test_menu_actions_running_ide_enabled_only():
    actions = dashboard.menu_actions(_ctx(apps=_apps("ide")))
    assert _session_verbs(actions) == [
        "tmux",
        "shell",
        "ide",
        "net loose",
        "net egress ls",
        "restart",
        "stop",
        "destroy",
    ]
    assert "chrome" not in [a for _, a in actions]


def test_menu_actions_running_chrome_enabled_only():
    actions = dashboard.menu_actions(_ctx(apps=_apps("chrome")))
    assert _session_verbs(actions) == [
        "tmux",
        "shell",
        "chrome",
        "net loose",
        "net egress ls",
        "restart",
        "stop",
        "destroy",
    ]
    assert "ide" not in [a for _, a in actions]


def test_menu_actions_running_both_enabled():
    actions = dashboard.menu_actions(_ctx(apps=_apps("ide", "chrome")))
    assert _session_verbs(actions) == [
        "tmux",
        "shell",
        "ide",
        "chrome",
        "net loose",
        "net egress ls",
        "restart",
        "stop",
        "destroy",
    ]


def test_action_menu_lists_every_registry_app():
    """`menu_actions` renders whatever `ctx.apps` it is handed, in order —
    not just the two builtins that used to have their own booleans."""
    actions = dashboard.menu_actions(_ctx(apps=_apps("firefox", "figma")))
    verbs = [verb for _label, verb in actions]
    assert "firefox" in verbs
    assert "figma" in verbs
    assert ("Launch firefox", "firefox") in actions
    assert ("Launch figma", "figma") in actions
    assert verbs.index("firefox") < verbs.index("figma") < verbs.index("pr")


def test_remote_action_menu_offers_no_app_launches():
    """A GUI app would open on the host's display, not the SSH client's."""
    apps = _apps("ide", "chrome", "figma")
    local = dashboard.menu_actions(_ctx(apps=apps))
    remote = dashboard.menu_actions(_ctx(apps=apps, remote=True))

    assert {verb for _label, verb in local} >= {"ide", "chrome", "figma"}
    assert not {verb for _label, verb in remote} & {"ide", "chrome", "figma"}
    assert [a for a in local if a[1] not in {"ide", "chrome", "figma"}] == remote
    assert [
        item.label
        for item in dashboard.group_menu_actions(remote)
        if isinstance(item, dashboard.MenuGroup)
    ] == ["PR →", "Git →"]
    assert ("Egress…", "net egress ls") in remote


def test_remote_quick_keys_refuse_gui_apps_and_say_why():
    group = dashboard.RepoGroup("alpha", "/repos/alpha", None, [_ci("alpha-x", "alpha")])
    group.apps = _apps("ide", "chrome")

    assert dashboard.quick_verb([group], "alpha-x", "action:ide") == "ide"
    assert dashboard.quick_verb([group], "alpha-x", "action:ide", remote=True) is None
    assert dashboard.quick_verb([group], "alpha-x", "action:chrome", remote=True) is None
    assert dashboard.quick_verb([group], "alpha-x", "action:shell", remote=True) == "shell"
    note = dashboard.quick_reject_note([group], "alpha-x", "action:ide", remote=True)
    assert note == "GUI apps are not available over remote SSH"
    assert dashboard.open_menu([group], "alpha-x", remote=True) is not None


def test_action_menu_renders_a_builtins_description_as_its_label():
    """A builtin's `AppSpec.description` (e.g. "JetBrains idea") is real,
    user-facing English — the bare verb the earlier lowercase labels used is
    not what the registry actually carries."""
    actions = dashboard.menu_actions(_ctx(apps=[dashboard.AppMenuEntry("ide", "JetBrains idea")]))
    assert ("Launch JetBrains idea", "ide") in actions


def test_action_menu_falls_back_to_the_verb_when_description_is_empty():
    """A user's `apps:` entry that never set `description` must still render
    something readable, not a blank label."""
    actions = dashboard.menu_actions(_ctx(apps=[dashboard.AppMenuEntry("figma", "figma")]))
    assert ("Launch figma", "figma") in actions


def test_action_menu_has_no_apps_when_none_are_configured():
    actions = dashboard.menu_actions(_ctx(apps=[]))
    verbs = [verb for _label, verb in actions]
    assert "chrome" not in verbs and "ide" not in verbs


def test_menu_actions_stopped():
    actions = dashboard.menu_actions(_ctx(state="Stopped"))
    assert [a for _, a in actions] == ["start", "net egress ls", "destroy"]


def test_menu_actions_orphan_disabled():
    assert dashboard.menu_actions(_ctx(has_repo=False)) == []


def test_menu_actions_orphan_disabled_regardless_of_flags():
    assert dashboard.menu_actions(_ctx(has_repo=False, apps=_apps("ide", "chrome"))) == []


def test_menu_actions_unknown_state_only_destroy():
    assert [a for _, a in dashboard.menu_actions(_ctx(state="Frozen"))] == ["destroy"]


def test_menu_actions_running_network_strict_offers_loose():
    verbs = [a for _, a in dashboard.menu_actions(_ctx(current_network="strict"))]
    assert "net loose" in verbs
    assert "net strict" not in verbs


def test_menu_actions_running_network_loose_offers_strict():
    verbs = [a for _, a in dashboard.menu_actions(_ctx(current_network="loose"))]
    assert "net strict" in verbs
    assert "net loose" not in verbs


def test_menu_actions_running_network_unknown_offers_both():
    verbs = [a for _, a in dashboard.menu_actions(_ctx(current_network=None))]
    assert "net strict" in verbs
    assert "net loose" in verbs


def test_menu_actions_stopped_has_no_network_entries():
    verbs = [a for _, a in dashboard.menu_actions(_ctx(state="Stopped"))]
    assert verbs.count("net egress ls") == 1
    assert not any(v in {"net strict", "net loose"} for v in verbs)


def test_network_group_keeps_mode_eligibility_and_stopped_egress_view():
    running = dashboard.group_menu_actions(dashboard.menu_actions(_ctx()), include_network=True)
    network = next(
        item
        for item in running
        if isinstance(item, dashboard.MenuGroup) and item.label == "Network →"
    )
    assert [verb for _, verb in network.actions] == ["net loose", "net egress ls"]
    stopped = dashboard.group_menu_actions(
        dashboard.menu_actions(_ctx(state="Stopped")), include_network=True
    )
    network = next(
        item
        for item in stopped
        if isinstance(item, dashboard.MenuGroup) and item.label == "Network →"
    )
    assert network.actions == (("Egress…", "net egress ls"),)
    assert dashboard.group_menu_actions(dashboard.menu_actions(_ctx(has_repo=False))) == []


def test_egress_view_remote_policy_is_independent():
    from jailbee.config.models_remote import RemoteCommandPolicy, RemoteSSHConfig

    group = dashboard.RepoGroup("alpha", "/alpha", None, [_ci("alpha-1", "alpha")])
    view_only = RemoteSSHConfig(
        commands=RemoteCommandPolicy(mode="allowlist", allow=["net egress ls"])
    )
    actions = dashboard.actions_for_container(
        [group], "alpha-1", over_ssh=True, ssh_policy=view_only
    )
    assert [(label, verb) for label, verb in actions if verb.startswith("net ")] == [
        ("Egress…", "net egress ls")
    ]
    restricted_full = RemoteSSHConfig(commands=RemoteCommandPolicy(mode="full"), restrict_host=True)
    actions = dashboard.actions_for_container(
        [group], "alpha-1", over_ssh=True, ssh_policy=restricted_full
    )
    assert "net egress ls" in [verb for _, verb in actions]
    assert "net egress add" not in [verb for _, verb in actions]
    assert "net egress rm" not in [verb for _, verb in actions]


def test_repo_menu_offers_egress_only_for_actionable_repo():
    groups = [
        dashboard.RepoGroup("alpha", "/alpha", None, []),
        dashboard.RepoGroup("orphan", None, None, [_ci("orphan-1", "orphan")]),
    ]
    actionable = dashboard.open_repo_menu(groups, "alpha", frozenset())
    assert actionable is not None
    assert dashboard.MenuGroup("Network →", (("Egress…", "net egress ls"),)) in actionable.actions
    orphan = dashboard.open_repo_menu(groups, "orphan", frozenset())
    assert orphan is not None
    assert all(not isinstance(item, dashboard.MenuGroup) for item in orphan.actions)


def test_repo_menu_egress_respects_ssh_read_permission():
    from jailbee.config.models_remote import RemoteCommandPolicy, RemoteSSHConfig

    group = dashboard.RepoGroup("alpha", "/alpha", None, [])
    allowed = RemoteSSHConfig(
        commands=RemoteCommandPolicy(mode="allowlist", allow=["net egress ls"])
    )
    denied = RemoteSSHConfig(commands=RemoteCommandPolicy(mode="disabled"))
    allowed_menu = dashboard.open_repo_menu(
        [group], "alpha", frozenset(), ssh_policy=allowed, over_ssh=True
    )
    denied_menu = dashboard.open_repo_menu(
        [group], "alpha", frozenset(), ssh_policy=denied, over_ssh=True
    )
    assert allowed_menu is not None and any(
        isinstance(item, dashboard.MenuGroup) and item.label == "Network →"
        for item in allowed_menu.actions
    )
    assert denied_menu is not None and all(
        not isinstance(item, dashboard.MenuGroup) for item in denied_menu.actions
    )


def test_repo_network_menu_is_a_submenu_and_escape_returns_to_parent():
    group = dashboard.RepoGroup("alpha", "/alpha", None, [])
    menu = dashboard.open_repo_menu([group], "alpha", frozenset())
    assert menu is not None
    assert menu.actions[4] == dashboard.MenuGroup("Network →", (("Egress…", "net egress ls"),))

    menu.index = 4
    child, verb = dashboard.enter_menu(menu)
    assert verb is None
    assert isinstance(child, dashboard.RepoMenuState)
    assert child.active_group == "Network →"
    assert dashboard.menu_verb(child) == "net egress ls"
    parent = dashboard.back_menu(child)
    assert parent is not None
    assert parent.active_group is None
    assert parent.index == 4
    assert dashboard.menu_verb(parent) is None


def test_repo_network_submenu_remains_gated_by_ssh_read_permission():
    from jailbee.config.models_remote import RemoteCommandPolicy, RemoteSSHConfig

    group = dashboard.RepoGroup("alpha", "/alpha", None, [])
    denied = RemoteSSHConfig(commands=RemoteCommandPolicy(mode="disabled"))
    menu = dashboard.open_repo_menu([group], "alpha", frozenset(), ssh_policy=denied, over_ssh=True)
    assert menu is not None
    assert all(not isinstance(item, dashboard.MenuGroup) for item in menu.actions)


def _container_egress_keys(group: dashboard.RepoGroup, **menu_kwargs) -> list[bytes]:
    """Keys that open the first container's Egress panel from the dashboard.

    ``menu_kwargs`` (``remote``/``over_ssh``/``ssh_policy``) must match the
    ``run()`` call: the menu an SSH session sees has other entries.
    """
    menu = dashboard.open_menu([group], group.containers[0].name, **menu_kwargs)
    assert menu is not None
    root = dashboard._menu_entries(menu)
    network_index = next(
        i
        for i, item in enumerate(root)
        if isinstance(item, dashboard.MenuGroup) and item.label == "Network →"
    )
    network = root[network_index]
    assert isinstance(network, dashboard.MenuGroup)
    egress_index = next(i for i, (_, verb) in enumerate(network.actions) if verb == "net egress ls")
    return [
        b"j",
        b"\r",
        *([b"j"] * network_index),
        b"\r",
        *([b"j"] * egress_index),
        b"\r",
    ]


def test_egress_add_prompts_inline_then_runs_the_scoped_cli(mocker, tmp_path):
    group = dashboard.RepoGroup("alpha", str(tmp_path), None, [_ci("alpha-x", "alpha")])
    rows = mocker.patch.object(dashboard, "load_egress_rows", return_value=())
    prompt = mocker.patch("typer.prompt")
    child = mocker.patch.object(dashboard.subprocess, "run")
    child.return_value.returncode = 0
    mocker.patch.object(dashboard, "_wait_for_return")
    render = mocker.patch.object(dashboard, "render", wraps=dashboard.render)

    keys = [*_container_egress_keys(group), b"a", *_keys("example.com:8443"), _ENTER, b"\x03"]
    assert _drive_run(mocker, keys, groups=[group]) == 0

    assert rows.call_count == 1  # a change that worked closes the panel: no reload
    assert rows.call_args_list[0].args[0::2] == (tmp_path, "alpha-x")
    prompt.assert_not_called()
    child.assert_called_once_with(
        ["jailbee", "net", "egress", "add", "example.com:8443", "alpha-x"],
        check=False,
        cwd=tmp_path,
    )
    calls = render.call_args_list
    asked = [
        i
        for i, call in enumerate(calls)
        if isinstance(call.kwargs.get("overlay"), dashboard.TextPrompt)
        and call.kwargs["overlay"].purpose == "egress-add"
    ]
    assert asked, "the destination question was never drawn in the frame"
    # While the question is open the cursor stays on the container the panel
    # is about, not the repo header.
    assert calls[asked[0]].args[1] == dashboard.Row("container", "alpha-x")
    # After the submit the whole stack is gone — panel and the menu behind it —
    # so the next key acts on the table, not on a stale Esc chain.
    assert calls[-1].kwargs.get("overlay") is None


def test_ssh_egress_container_panel_can_remove_but_not_add_without_network(mocker, tmp_path):
    from jailbee.config.models_remote import RemoteCommandPolicy, RemoteSSHConfig

    group = dashboard.RepoGroup("alpha", str(tmp_path), None, [_ci("alpha-x", "alpha")])
    policy = RemoteSSHConfig(commands=RemoteCommandPolicy(mode="full"), restrict_host=True)
    menu = dashboard.open_menu([group], "alpha-x", remote=True, over_ssh=True, ssh_policy=policy)
    assert menu is not None
    root = dashboard._menu_entries(menu)
    network_index = next(
        i
        for i, item in enumerate(root)
        if isinstance(item, dashboard.MenuGroup) and item.label == "Network →"
    )
    network = next(
        item for item in root if isinstance(item, dashboard.MenuGroup) and item.label == "Network →"
    )
    egress_index = next(i for i, (_, verb) in enumerate(network.actions) if verb == "net egress ls")
    mocker.patch.object(
        dashboard, "load_egress_rows", return_value=(EntryRow("allowed.example", "config"),)
    )
    prompt = mocker.patch("typer.prompt")
    child = mocker.patch.object(dashboard.subprocess, "run")
    render = mocker.patch.object(dashboard, "render", wraps=dashboard.render)

    assert (
        _drive_run(
            mocker,
            [
                b"j",
                b"\r",
                *([b"j"] * network_index),
                b"\r",
                *([b"j"] * egress_index),
                b"\r",
                b"a",
                b"\x1b",
                b"\x03",
            ],
            groups=[group],
            remote=True,
            over_ssh=True,
            ssh_policy=policy,
        )
        == 0
    )

    prompt.assert_not_called()
    child.assert_not_called()
    panels = [call.kwargs["overlay"] for call in render.call_args_list]
    egress = next(panel for panel in panels if isinstance(panel, dashboard.EgressState))
    assert egress.can_add is False
    assert egress.can_rm is True
    # A refused add never opens the destination question.
    assert not any(isinstance(panel, dashboard.TextPrompt) for panel in panels)
    assert any(
        "net egress add is not permitted" in str(call.kwargs.get("notice"))
        for call in render.call_args_list
    )


@pytest.mark.parametrize("network", [False, True])
def test_ssh_egress_permission_by_scope_and_network_switch(network: bool) -> None:
    from jailbee.config.models_remote import RemoteCommandPolicy, RemoteSSHConfig
    from jailbee.dashboard_commands import permitted
    from jailbee.dashboard_egress import EgressState, egress_argv

    policy = RemoteSSHConfig(commands=RemoteCommandPolicy(mode="full"), network=network)
    container = EgressState("alpha", "alpha-1", ())
    repo = EgressState("alpha", None, ())

    def ok(state: EgressState, action: str) -> bool:
        return permitted(egress_argv(state, action, "example.com"), policy, over_ssh=True)

    assert ok(container, "add") is network
    assert ok(container, "rm") is True
    assert ok(repo, "add") is False
    assert ok(repo, "rm") is False


@pytest.mark.parametrize("network", [False, True])
def test_ssh_action_menu_offers_loose_only_with_the_network_switch(network: bool) -> None:
    from jailbee.config.models_remote import RemoteCommandPolicy, RemoteSSHConfig

    group = dashboard.RepoGroup("alpha", "/alpha", None, [_ci("alpha-1", "alpha")])
    local = [verb for _, verb in dashboard.actions_for_container([group], "alpha-1")]
    assert "net loose" in local
    policy = RemoteSSHConfig(commands=RemoteCommandPolicy(mode="full"), network=network)
    verbs = [
        verb
        for _, verb in dashboard.actions_for_container(
            [group], "alpha-1", over_ssh=True, ssh_policy=policy
        )
    ]
    assert ("net loose" in verbs) is network


def test_run_removes_only_selected_container_override(mocker, tmp_path):
    group = dashboard.RepoGroup("alpha", str(tmp_path), None, [_ci("alpha-x", "alpha")])
    rows = (
        EntryRow("from-config.example", "config"),
        EntryRow("container-only.example", "container"),
    )
    load = mocker.patch.object(dashboard, "load_egress_rows", return_value=rows)
    child = mocker.patch.object(dashboard.subprocess, "run")
    child.return_value.returncode = 0
    mocker.patch.object(dashboard, "_wait_for_return")
    menu = dashboard.open_menu([group], "alpha-x")
    assert menu is not None
    root = dashboard._menu_entries(menu)
    network_index = next(
        i
        for i, item in enumerate(root)
        if isinstance(item, dashboard.MenuGroup) and item.label == "Network →"
    )
    network = next(
        item for item in root if isinstance(item, dashboard.MenuGroup) and item.label == "Network →"
    )
    egress_index = next(i for i, (_, verb) in enumerate(network.actions) if verb == "net egress ls")

    keys = [
        b"j",
        b"\r",
        *([b"j"] * network_index),
        b"\r",
        *([b"j"] * egress_index),
        b"\r",
        b"j",
        b"r",
        b"\x03",
    ]
    render = mocker.patch.object(dashboard, "render", wraps=dashboard.render)
    assert _drive_run(mocker, keys, groups=[group]) == 0

    assert load.call_count == 1  # a removal that worked closes the panel: no reload
    assert render.call_args_list[-1].kwargs["overlay"] is None
    child.assert_called_once_with(
        ["jailbee", "net", "egress", "rm", "container-only.example", "alpha-x"],
        check=False,
        cwd=tmp_path,
    )


def test_repo_egress_dispatch_uses_repo_scope_and_explicit_config(mocker, tmp_path):
    config_path = tmp_path / ".jailbee" / "config.yaml"
    group = dashboard.RepoGroup("alpha", str(tmp_path), config_path, [_ci("alpha-x", "alpha")])
    mocker.patch.object(dashboard, "load_egress_rows", return_value=())
    prompt = mocker.patch("typer.prompt")
    mocker.patch.object(dashboard, "_wait_for_return")
    child = mocker.patch.object(dashboard.subprocess, "run")
    child.return_value.returncode = 0

    keys = [
        b"\r",
        *[b"j"] * 4,  # past New container…, New from PR…, Credential group…, Accounts…
        b"\r",
        b"\r",
        b"a",
        *_keys("repo.example:443"),
        _ENTER,
        b"\x03",
    ]
    assert _drive_run(mocker, keys, groups=[group]) == 0

    prompt.assert_not_called()
    child.assert_called_once_with(
        [
            "jailbee",
            "net",
            "egress",
            "add",
            "repo.example:443",
            "--repo",
            "--config",
            str(config_path),
        ],
        check=False,
        cwd=tmp_path,
    )


@pytest.mark.parametrize("cancel", [b"\x1b", b"\x03"], ids=["escape", "ctrl-c"])
def test_egress_add_escape_returns_to_the_panel_with_a_notice(mocker, tmp_path, cancel):
    group = dashboard.RepoGroup("alpha", str(tmp_path), None, [_ci("alpha-x", "alpha")])
    mocker.patch.object(dashboard, "load_egress_rows", return_value=())
    prompt = mocker.patch("typer.prompt")
    child = mocker.patch.object(dashboard.subprocess, "run")
    render = mocker.patch.object(dashboard, "render", wraps=dashboard.render)

    keys = [*_container_egress_keys(group), b"a", *_keys("x"), cancel, b"\x03"]
    assert _drive_run(mocker, keys, groups=[group]) == 0

    prompt.assert_not_called()
    child.assert_not_called()
    calls = render.call_args_list
    assert any(
        isinstance(call.kwargs.get("overlay"), dashboard.TextPrompt)
        and call.kwargs["overlay"].text == "x"
        for call in calls
    ), "the typed text never reached the inline prompt"
    # Cancelling answers the question, not the dashboard: the panel is back.
    last = calls[-1].kwargs.get("overlay")
    assert isinstance(last, dashboard.EgressState)
    assert last.container == "alpha-x"
    assert calls[-1].kwargs.get("notice") == "Egress change cancelled"


def test_egress_add_rechecks_the_ssh_policy_at_submit(mocker, tmp_path):
    from jailbee.config.models_remote import RemoteCommandPolicy, RemoteSSHConfig

    group = dashboard.RepoGroup("alpha", str(tmp_path), None, [_ci("alpha-x", "alpha")])
    # `net egress add` is a host command: only reachable with restrict_host off.
    policy = RemoteSSHConfig(
        commands=RemoteCommandPolicy(mode="allowlist", allow=["net egress ls", "net egress add"]),
        restrict_host=False,
    )
    mocker.patch.object(dashboard, "load_egress_rows", return_value=())
    child = mocker.patch.object(dashboard.subprocess, "run")
    render = mocker.patch.object(dashboard, "render", wraps=dashboard.render)
    _mock_terminal(mocker)
    _fake_state(mocker, [group])
    mocker.patch.object(dashboard.select, "select", return_value=([True], [], []))

    def revoke_add() -> bytes:
        # The operator narrows the policy while the question is open.
        policy.commands.allow[:] = ["net egress ls"]
        return b"m"

    script = iter(
        [
            *_container_egress_keys(group, remote=True, over_ssh=True, ssh_policy=policy),
            b"a",
            *_keys("example.co"),
            revoke_add,
            _ENTER,
        ]
    )

    def read(_fd, _n):
        step = next(script, b"\x03")
        return step() if callable(step) else step

    mocker.patch.object(dashboard.os, "read", side_effect=read)
    dashboard.run(
        mocker.Mock(),
        None,
        remote=True,
        over_ssh=True,
        ssh_policy=policy,
    )

    calls = render.call_args_list
    assert any(
        isinstance(call.kwargs.get("overlay"), dashboard.TextPrompt)
        and call.kwargs["overlay"].text == "example.com"
        for call in calls
    ), "the add question never opened under the permissive policy"
    child.assert_not_called()
    assert any(
        call.kwargs.get("notice") == "net egress add is not permitted by the SSH policy"
        for call in calls
    )
    assert isinstance(calls[-1].kwargs.get("overlay"), dashboard.EgressState)


def test_egress_add_blank_destination_is_rejected_inline(mocker, tmp_path):
    group = dashboard.RepoGroup("alpha", str(tmp_path), None, [_ci("alpha-x", "alpha")])
    mocker.patch.object(dashboard, "load_egress_rows", return_value=())
    child = mocker.patch.object(dashboard.subprocess, "run")
    render = mocker.patch.object(dashboard, "render", wraps=dashboard.render)

    keys = [*_container_egress_keys(group), b"a", *_keys("  "), _ENTER, b"\x03"]
    assert _drive_run(mocker, keys, groups=[group]) == 0

    child.assert_not_called()
    # Enter keeps the question open with the reason (the trailing Ctrl-C
    # then cancels it, so this is not the last frame).
    rejected = [
        call.kwargs["overlay"]
        for call in render.call_args_list
        if isinstance(call.kwargs.get("overlay"), dashboard.TextPrompt)
        and call.kwargs["overlay"].error is not None
    ]
    assert [(p.purpose, p.error) for p in rejected] == [
        ("egress-add", "Destination (host, host:port, *.domain, IPv4, or CIDR) cannot be empty")
    ]


@pytest.mark.parametrize("returncode", [1, 2], ids=["mutation-failure", "invalid-destination"])
def test_egress_mutation_failure_is_visible(mocker, tmp_path, returncode):
    group = dashboard.RepoGroup("alpha", str(tmp_path), None, [_ci("alpha-x", "alpha")])
    mocker.patch.object(dashboard, "load_egress_rows", return_value=())
    mocker.patch.object(dashboard, "_wait_for_return")
    child = mocker.patch.object(dashboard.subprocess, "run")
    child.return_value.returncode = returncode
    render = mocker.patch.object(dashboard, "render", wraps=dashboard.render)

    keys = [*_container_egress_keys(group), b"a", *_keys("invalid..example"), _ENTER, b"\x03"]
    assert _drive_run(mocker, keys, groups=[group]) == 0

    # Destination validation stays the CLI's: the dashboard passes it through.
    child.assert_called_once_with(
        ["jailbee", "net", "egress", "add", "invalid..example", "alpha-x"],
        check=False,
        cwd=tmp_path,
    )
    assert any(
        f"exited {returncode}" in str(call.kwargs.get("notice", ""))
        for call in render.call_args_list
    )


def test_egress_panel_closes_when_container_disappears(mocker, tmp_path):
    container = _ci("alpha-x", "alpha")
    group = dashboard.RepoGroup("alpha", str(tmp_path), None, [container])

    def remove_container_while_typing() -> bytes:
        # The container goes away after the question opened, before Enter;
        # the key loop draws at least one frame in between.
        group.containers.clear()
        return b"x"

    script = iter(
        [
            *_container_egress_keys(group),
            b"a",
            *_keys("example.com"),
            remove_container_while_typing,
            _ENTER,
        ]
    )

    def read(_fd, _n):
        step = next(script, b"\x03")
        return step() if callable(step) else step

    mocker.patch.object(dashboard, "load_egress_rows", return_value=())
    child = mocker.patch.object(dashboard.subprocess, "run")
    render = mocker.patch.object(dashboard, "render", wraps=dashboard.render)

    assert _drive_run_with_reader(mocker, read, [group]) == 0

    child.assert_not_called()
    calls = render.call_args_list
    assert any(
        isinstance(call.kwargs.get("overlay"), dashboard.TextPrompt)
        and call.kwargs["overlay"].text == "example.comx"
        for call in calls
    ), "the prompt must still be open, with the text typed after the removal"
    overlays = [call.kwargs.get("overlay") for call in calls]
    assert not isinstance(overlays[-1], (dashboard.EgressState, dashboard.TextPrompt))
    assert any(
        call.kwargs.get("notice") == "Egress target is no longer available" for call in calls
    )


def test_egress_panel_closes_when_repo_disappears_during_dispatch(mocker, tmp_path):
    group = dashboard.RepoGroup("alpha", str(tmp_path), None, [_ci("alpha-x", "alpha")])
    mocker.patch.object(dashboard, "load_egress_rows", return_value=())
    child = mocker.patch.object(dashboard.subprocess, "run", side_effect=FileNotFoundError())
    render = mocker.patch.object(dashboard, "render", wraps=dashboard.render)

    keys = [*_container_egress_keys(group), b"a", *_keys("example.com"), _ENTER, b"\x03"]
    assert _drive_run(mocker, keys, groups=[group]) == 0

    child.assert_called_once()
    assert child.call_args.args[0] == ["jailbee", "net", "egress", "add", "example.com", "alpha-x"]
    overlays = [call.kwargs.get("overlay") for call in render.call_args_list]
    assert not isinstance(overlays[-1], dashboard.EgressState)
    assert any(
        "no longer exists" in str(call.kwargs.get("notice", "")) for call in render.call_args_list
    )


def test_egress_loader_failure_is_visible_and_does_not_crash(mocker, tmp_path):
    group = dashboard.RepoGroup("alpha", str(tmp_path), None, [_ci("alpha-x", "alpha")])
    menu = dashboard.open_menu([group], "alpha-x")
    assert menu is not None
    root = dashboard._menu_entries(menu)
    network_index = next(
        i
        for i, item in enumerate(root)
        if isinstance(item, dashboard.MenuGroup) and item.label == "Network →"
    )
    network = next(
        item for item in root if isinstance(item, dashboard.MenuGroup) and item.label == "Network →"
    )
    egress_index = next(i for i, (_, verb) in enumerate(network.actions) if verb == "net egress ls")
    mocker.patch.object(
        dashboard, "load_egress_rows", side_effect=LookupError("database unavailable")
    )
    render = mocker.patch.object(dashboard, "render", wraps=dashboard.render)

    assert (
        _drive_run(
            mocker,
            [
                b"j",
                b"\r",
                *([b"j"] * network_index),
                b"\r",
                *([b"j"] * egress_index),
                b"\r",
                b"\x03",
            ],
            groups=[group],
        )
        == 0
    )

    assert any(
        "could not load egress entries" in str(call.kwargs.get("notice"))
        for call in render.call_args_list
    )


def test_menu_actions_orphan_disabled_even_with_network():
    assert dashboard.menu_actions(_ctx(has_repo=False, current_network="strict")) == []


def test_menu_actions_network_entries_ordered_after_chrome_before_restart():
    actions = dashboard.menu_actions(_ctx(apps=_apps("ide", "chrome")))
    assert _session_verbs(actions) == [
        "tmux",
        "shell",
        "ide",
        "chrome",
        "net loose",
        "net egress ls",
        "restart",
        "stop",
        "destroy",
    ]
    verb_to_label = {verb: label for label, verb in actions}
    assert verb_to_label["net loose"] == "Network: loose"


def test_menu_actions_running_includes_open_pr_when_pr_known():
    actions = dashboard.menu_actions(_ctx(pr_number=123))
    assert [verb for _, verb in actions[:5]] == [
        "tmux",
        "shell",
        "outbox browse",
        "pr --open",
        "pr",
    ]


def test_menu_actions_stopped_includes_open_pr_when_pr_known():
    actions = dashboard.menu_actions(_ctx(state="Stopped", pr_number=7))
    assert actions == [
        ("Start", "start"),
        ("Open PR", "pr --open"),
        ("Egress…", "net egress ls"),
        ("Destroy", "destroy"),
    ]
    assert dashboard.group_menu_actions(actions, include_network=True) == [
        ("Start", "start"),
        dashboard.MenuGroup("PR →", (("Open PR", "pr --open"),)),
        dashboard.MenuGroup("Network →", (("Egress…", "net egress ls"),)),
        ("Destroy", "destroy"),
    ]


def test_menu_actions_omits_open_pr_when_no_pr():
    running = dashboard.menu_actions(_ctx())
    stopped = dashboard.menu_actions(_ctx(state="Stopped"))
    assert ("Open PR", "pr --open") not in running
    assert ("Open PR", "pr --open") not in stopped


def test_menu_actions_orphan_stays_empty_even_with_pr():
    assert dashboard.menu_actions(_ctx(has_repo=False, pr_number=123)) == []


def test_menu_actions_running_offers_the_workflow_verbs():
    """Sessions lead, then PR and Git; unknown Git status retains its leaves."""
    verbs = [v for _, v in dashboard.menu_actions(_ctx())]
    assert verbs == [
        "tmux",
        "shell",
        "outbox browse",
        "pr",
        "merge",
        "git pull",
        "git push",
        "git retarget",
        "git diff",
        "net loose",
        "net egress ls",
        "restart",
        "stop",
        "destroy",
    ]


def test_group_menu_actions_separates_git_and_pr():
    leaves = [
        ("Attach tmux", "tmux"),
        ("Open PR", "pr --open"),
        ("Create/update PR", "pr"),
        ("Apply PR actions", "review apply"),
        ("Merge into…", "merge"),
        ("Send commits to host", "git pull"),
        ("Update from base", "git push"),
        ("Refresh from PR head", "git push --pr"),
        ("Show diff", "git diff"),
    ]
    grouped = dashboard.group_menu_actions(leaves)
    assert [
        item.label if isinstance(item, dashboard.MenuGroup) else item[0] for item in grouped
    ] == ["Attach tmux", "PR →", "Git →"]
    assert grouped[1] == dashboard.MenuGroup("PR →", tuple(leaves[1:4]))
    assert grouped[2] == dashboard.MenuGroup("Git →", tuple(leaves[4:]))
    assert dashboard.group_menu_actions([]) == []


def test_group_menu_actions_collects_registry_launches_at_first_occurrence():
    leaves = [
        ("Attach tmux", "tmux"),
        ("Launch Chrome", "chrome"),
        ("Open shell", "shell"),
        ("Launch Figma", "apps run figma --container"),
        ("Create/update PR", "pr"),
        ("Show diff", "git diff"),
    ]
    assert dashboard.group_menu_actions(leaves) == [
        leaves[0],
        dashboard.MenuGroup("Launch →", (leaves[1], leaves[3])),
        leaves[2],
        dashboard.MenuGroup("PR →", (leaves[4],)),
        dashboard.MenuGroup("Git →", (leaves[5],)),
    ]


def test_group_menu_actions_keeps_relative_order_and_unclassified_leaves():
    leaves = [
        ("Show diff", "git diff"),
        ("Launch git-tool", "apps run git-tool --container"),
        ("Apply issue actions", "issue apply"),
        ("Open PR", "pr --open"),
        ("Merge into…", "merge"),
        ("Create/update PR", "pr"),
    ]
    assert dashboard.group_menu_actions(leaves) == [
        dashboard.MenuGroup("Git →", (leaves[0], leaves[4])),
        dashboard.MenuGroup("Launch →", (leaves[1],)),
        leaves[2],
        dashboard.MenuGroup("PR →", (leaves[3], leaves[5])),
    ]


def test_menu_actions_mount_mode_keeps_outbox_without_git():
    grouped = dashboard.group_menu_actions(
        dashboard.menu_actions(_ctx(mode="mount")), include_network=True
    )
    assert ("Outbox", "outbox browse") in grouped
    assert [item.label for item in grouped if isinstance(item, dashboard.MenuGroup)] == [
        "Network →"
    ]


def test_grouped_git_leaves_respect_known_clean_and_unknown_status():
    clean = _dirty(wt="clean", ahead_diff="clean", ahead_count="0")
    unknown = _dirty(wt="?", ahead_diff="?", ahead_count="?")
    for status, expected in (
        (clean, ["merge", "git push", "git retarget"]),
        (unknown, ["merge", "git pull", "git push", "git retarget", "git diff"]),
        (None, ["merge", "git pull", "git push", "git retarget", "git diff"]),
    ):
        grouped = dashboard.group_menu_actions(dashboard.menu_actions(_ctx(git_status=status)))
        git_group = next(
            item
            for item in grouped
            if isinstance(item, dashboard.MenuGroup) and item.label == "Git →"
        )
        assert [verb for _, verb in git_group.actions] == expected


def test_menu_actions_workflow_labels_name_their_verb():
    labels = {verb: label for label, verb in dashboard.menu_actions(_ctx())}
    assert labels["pr"] == "Create/update PR"
    assert labels["git push"] == "Update from base (git push)"
    assert labels["git pull"] == "Send commits to host (git pull)"
    assert labels["git diff"] == "Show diff (git diff)"
    assert labels["git retarget"] == "Change base branch (git retarget)"
    assert dashboard.dispatch_style("git retarget") == "output"


def test_menu_actions_offers_pr_refresh_on_a_review_container():
    """A container built from someone else's PR can pull in commits the author
    pushed since, so the entry sits right after the base-update it mirrors."""
    actions = dashboard.menu_actions(_ctx(pr_number=123))
    verbs = [v for _, v in actions]
    assert verbs[verbs.index("git push") + 1] == "git push --pr"
    labels = {verb: label for label, verb in actions}
    assert labels["git push --pr"] == "Refresh from PR head (git push --pr)"


def test_menu_actions_omits_pr_refresh_on_an_authored_pr():
    """`pr_author` means jailbee opened the PR from this container's branch, so
    its head is downstream of the container and a refresh is a no-op."""
    actions = dashboard.menu_actions(_ctx(pr_number=123, pr_author=True))
    verbs = [v for _, v in actions]
    assert "git push --pr" not in verbs
    assert "pr --open" in verbs  # the PR itself is still reachable


def test_menu_actions_omits_pr_refresh_without_a_pr():
    assert "git push --pr" not in [v for _, v in dashboard.menu_actions(_ctx())]


def test_menu_actions_omits_pr_refresh_when_the_bridge_is_impossible():
    """No clone to push into: `jailbee git push` would fail in
    `sync.assert_container_publishable` on either of these."""
    for ctx in (_ctx(state="Stopped", pr_number=5), _ctx(mode="mount", pr_number=5)):
        assert "git push --pr" not in [v for _, v in dashboard.menu_actions(ctx)]


@pytest.mark.parametrize("count", [None, 2])
@pytest.mark.parametrize("mode", ["clone", "mount"])
def test_menu_has_one_outbox_unless_both_outboxes_are_empty(count, mode):
    actions = dashboard.menu_actions(
        _ctx(mode=mode, git_status=_dirty(pending_pr_actions=count, pending_issue_actions=count))
    )
    assert [v for _, v in actions].count("outbox browse") == 1
    assert not {"review apply", "issue apply"} & {v for _, v in actions}
    for ctx in (_ctx(state="Stopped", mode=mode), _ctx(has_repo=False, mode=mode)):
        assert "outbox browse" not in {v for _, v in dashboard.menu_actions(ctx)}


@pytest.mark.parametrize("mode", ["clone", "mount"])
def test_an_empty_outbox_is_not_offered(mode):
    empty = _dirty(pending_pr_actions=0, pending_issue_actions=0)
    actions = dashboard.menu_actions(_ctx(mode=mode, git_status=empty))
    assert "outbox browse" not in {v for _, v in actions}
    assert actions[:2] == [("Attach tmux", "tmux"), ("Open shell", "shell")]


@pytest.mark.parametrize(("pr", "issue"), [(0, None), (None, 0)])
def test_one_unknown_outbox_count_still_offers_the_outbox(pr, issue):
    status = _dirty(pending_pr_actions=pr, pending_issue_actions=issue)
    assert ("Outbox", "outbox browse") in dashboard.menu_actions(_ctx(git_status=status))


@pytest.mark.parametrize("git_status", [None, _dirty()])
def test_outbox_follows_the_shell_when_the_count_is_unknown(git_status):
    actions = dashboard.menu_actions(_ctx(git_status=git_status))
    assert actions[:3] == [
        ("Attach tmux", "tmux"),
        ("Open shell", "shell"),
        ("Outbox", "outbox browse"),
    ]
    menu = dashboard.MenuState("alpha-x", actions)
    assert dashboard._menu_entries(menu)[0] == ("Attach tmux", "tmux")


@pytest.mark.parametrize(("pr", "issue", "total"), [(2, None, 2), (None, 1, 1), (2, 1, 3)])
def test_pending_outbox_leads_the_menu_with_its_count(pr, issue, total):
    actions = dashboard.menu_actions(
        _ctx(git_status=_dirty(pending_pr_actions=pr, pending_issue_actions=issue))
    )
    lead = (f"Outbox ({total} pending)", "outbox browse")
    assert actions[:3] == [lead, ("Attach tmux", "tmux"), ("Open shell", "shell")]
    menu = dashboard.MenuState("alpha-x", actions)
    assert dashboard._menu_entries(menu)[0] == lead


def test_outbox_dispatch_uses_target_config_and_no_pause(mocker, tmp_path):
    run = mocker.patch.object(dashboard.subprocess, "run")
    run.return_value.returncode = 0
    pause = mocker.patch.object(dashboard, "_wait_for_return")
    dashboard._dispatch_action(_dispatch_target(tmp_path), "outbox browse", "alpha-x")
    run.assert_called_once_with(
        ["jailbee", "outbox", "browse", "alpha-x", "--config", str(tmp_path / "config.yaml")],
        check=False,
        cwd=tmp_path,
    )
    pause.assert_not_called()


def test_pr_refresh_is_dispatched_as_a_printing_verb():
    """PRINTING_VERBS is matched exactly, not by leading token — without its
    own entry the refresh would lose its output in both front-ends."""
    assert "git push --pr" in dashboard.PRINTING_VERBS
    assert dashboard.dispatch_style("git push --pr") == "output"


def test_menu_actions_mount_mode_has_no_workflow_verbs():
    """A mount-mode container has no clone of its own, so every one of these
    would fail in `sync.assert_container_publishable`."""
    verbs = [v for _, v in dashboard.menu_actions(_ctx(mode="mount"))]
    assert verbs == [
        "tmux",
        "shell",
        "outbox browse",
        "net loose",
        "net egress ls",
        "restart",
        "stop",
        "destroy",
    ]


def test_menu_actions_stopped_has_no_workflow_verbs():
    verbs = [v for _, v in dashboard.menu_actions(_ctx(state="Stopped"))]
    assert verbs == ["start", "net egress ls", "destroy"]


def test_menu_actions_hides_git_pull_when_nothing_is_ahead():
    verbs = [v for _, v in dashboard.menu_actions(_ctx(git_status=_dirty(ahead_count="0")))]
    assert "git pull" not in verbs
    assert "git diff" in verbs  # the working tree is still dirty
    assert "git push" in verbs  # "is the host ahead?" is not knowable here


def test_menu_actions_hides_git_diff_when_there_is_nothing_to_show():
    clean = _dirty(wt="clean", ahead_diff="clean", ahead_count="0")
    verbs = [v for _, v in dashboard.menu_actions(_ctx(git_status=clean))]
    assert "git diff" not in verbs
    assert "git pull" not in verbs


def test_menu_actions_shows_git_verbs_when_the_status_is_unknown():
    """`--no-git`, a base-tier refresh, or a failed probe must not silently
    remove actions — only a known no-op hides one."""
    unknown = _dirty(wt="?", ahead_diff="?", ahead_count="?")
    for status in (None, unknown):
        verbs = [v for _, v in dashboard.menu_actions(_ctx(git_status=status))]
        assert "git pull" in verbs
        assert "git diff" in verbs


def test_menu_actions_job_log_only_when_there_is_a_job():
    assert "job log" not in [v for _, v in dashboard.menu_actions(_ctx())]
    finished = dashboard.menu_actions(_ctx(has_job=True))
    assert ("Job log", "job log") in finished
    live = dashboard.menu_actions(_ctx(has_job=True, job_running=True))
    assert ("Job log", "job log --follow") in live


def test_menu_actions_job_log_precedes_the_pr_entries():
    """Sessions first, then diagnostics before PR and Git leaves."""
    verbs = [
        v for _, v in dashboard.menu_actions(_ctx(job_clearable=True, has_job=True, pr_number=7))
    ]
    assert verbs[:7] == [
        "tmux",
        "shell",
        "outbox browse",
        "job clear",
        "job log",
        "pr --open",
        "pr",
    ]


def test_menu_actions_orphan_ignores_every_workflow_field():
    assert (
        dashboard.menu_actions(_ctx(has_repo=False, has_job=True, pr_number=7, git_status=_dirty()))
        == []
    )


def test_open_menu_captures_the_actions_with_the_cursor_at_the_top(tmp_path):
    config_path = tmp_path / "config.yaml"
    group = dashboard.RepoGroup("alpha", str(tmp_path), config_path, [_ci("alpha-x", "alpha")])

    menu = dashboard.open_menu([group], "alpha-x")

    assert menu is not None
    assert menu.container == "alpha-x"
    assert menu.index == 0
    # the shared (Qt too) action list, plus the terminal-only entries
    assert [a for a in menu.actions if a[1] not in dashboard.TERMINAL_MENU_VERBS] == (
        dashboard.actions_for_container([group], "alpha-x")
    )
    assert ("Attach tmux", "tmux") in menu.actions


def test_open_menu_is_none_for_a_view_only_group():
    """A config-less (orphan) group has no actions, so there is no menu to open.

    The caller shows `view_only_note` instead — an empty menu panel would be
    indistinguishable from a broken one.
    """
    group = dashboard.RepoGroup("gamma", None, None, [_ci("gamma-x", "gamma")])

    assert dashboard.open_menu([group], "gamma-x") is None


def test_open_menu_is_none_for_an_unknown_or_unset_container(tmp_path):
    group = dashboard.RepoGroup(
        "alpha", str(tmp_path), tmp_path / "config.yaml", [_ci("alpha-x", "alpha")]
    )

    assert dashboard.open_menu([group], "alpha-nope") is None
    assert dashboard.open_menu([group], None) is None


def test_move_menu_clamps_at_both_edges():
    menu = dashboard.MenuState("alpha-x", [("A", "a"), ("B", "b"), ("C", "c")], index=0)

    assert dashboard.move_menu(menu, -1).index == 0  # already at the top
    assert dashboard.move_menu(menu, 1).index == 1
    assert dashboard.move_menu(dashboard.move_menu(menu, 1), 1).index == 2
    assert dashboard.move_menu(dashboard.MenuState("alpha-x", [], index=0), 1).index == 0


def test_move_menu_returns_a_new_state_and_leaves_the_original_alone():
    menu = dashboard.MenuState("alpha-x", [("A", "a"), ("B", "b")], index=0)

    moved = dashboard.move_menu(menu, 1)

    assert moved is not menu
    assert menu.index == 0


def test_menu_verb_returns_the_highlighted_verb():
    menu = dashboard.MenuState("alpha-x", [("A", "a"), ("B", "b")], index=1)

    assert dashboard.menu_verb(menu) == "b"
    assert dashboard.menu_verb(dashboard.MenuState("alpha-x", [], index=0)) is None


def _grouped_menu():
    return dashboard.MenuState(
        "alpha-x",
        [("Attach tmux", "tmux"), ("Create/update PR", "pr"), ("Show diff (git diff)", "git diff")],
    )


def test_menu_enters_groups_and_returns_to_saved_root_cursor():
    root = _grouped_menu()
    assert dashboard.back_menu(root) is None
    assert dashboard.menu_verb(dashboard.move_menu(root, 1)) is None
    assert dashboard.move_menu(root, -1).index == 0

    # Terminal order: Attach tmux, Git →, PR →.
    pr, verb = dashboard.enter_menu(dashboard.move_menu(dashboard.move_menu(root, 1), 1))
    assert verb is None
    assert pr.active_group == "PR →" and pr.index == 0 and pr.parent_index == 2
    assert dashboard.menu_verb(pr) == "pr"
    assert dashboard.enter_menu(pr) == (pr, "pr")
    assert dashboard.move_menu(pr, 1).index == 0

    parent = dashboard.back_menu(pr)
    assert parent is not None
    assert parent.active_group is None and parent.index == 2
    git, verb = dashboard.enter_menu(dashboard.move_menu(parent, -1))
    assert verb is None
    assert git.active_group == "Git →" and git.index == 0
    assert dashboard.enter_menu(git) == (git, "git diff")
    assert dashboard.back_menu(git).index == 1
    assert root.index == 0 and root.active_group is None


def test_menu_launch_submenu_navigates_and_dispatches_original_verbs():
    root = dashboard.MenuState(
        "alpha-x",
        [
            ("Attach tmux", "tmux"),
            ("Launch JetBrains idea", "ide"),
            ("Launch Figma", "apps run figma --container"),
            ("Destroy", "destroy"),
        ],
    )
    selected = dashboard.move_menu(root, 1)
    assert dashboard.menu_verb(selected) is None

    launch, verb = dashboard.enter_menu(selected)
    assert verb is None and launch.active_group == "Launch →"
    assert dashboard.menu_verb(launch) == "ide"
    assert dashboard.enter_menu(dashboard.move_menu(launch, 1))[1] == "apps run figma --container"
    parent = dashboard.back_menu(launch)
    assert parent is not None and parent.active_group is None and parent.index == 1


def test_menu_group_cursor_clamps_within_visible_entries():
    root = _grouped_menu()
    assert dashboard.move_menu(root, 10).index == 2
    git, _ = dashboard.enter_menu(dashboard.move_menu(root, 2))
    assert dashboard.move_menu(git, 10).index == 0
    assert dashboard.move_menu(git, -10).index == 0


def _hotkeys(menu: dashboard.MenuState | dashboard.RepoMenuState) -> dict[str, str | None]:
    entries = dashboard._menu_entries(menu)
    labels = [item.label if isinstance(item, dashboard.MenuGroup) else item[0] for item in entries]
    return dict(zip(labels, dashboard.menu_hotkeys(entries), strict=True))


def test_menu_hotkeys_give_running_root_entries_their_mnemonics():
    ctx = _ctx(pr_number=7, has_job=True, job_clearable=True)
    menu = dashboard.MenuState("alpha-x", dashboard.menu_actions(ctx))

    assert _hotkeys(menu) == {
        "Attach tmux": "t",
        "Outbox": "o",
        "Clear failed job": "x",
        "Job log": "b",
        "Git →": "g",
        "PR →": "p",
        "Lifecycle →": "l",
        "Network →": "w",
    }


def test_menu_hotkeys_inside_submenus_are_scoped_to_that_level():
    ctx = _ctx(pr_number=7)
    root = dashboard.MenuState("alpha-x", dashboard.menu_actions(ctx))

    def submenu(label: str) -> dashboard.MenuState:
        index = next(
            i
            for i, item in enumerate(dashboard._menu_entries(root))
            if isinstance(item, dashboard.MenuGroup) and item.label == label
        )
        child, _ = dashboard.enter_menu(dataclasses.replace(root, index=index))
        assert isinstance(child, dashboard.MenuState)
        return child

    assert _hotkeys(submenu("Git →")) == {
        "Merge into…": "m",
        "Send commits to host (git pull)": "l",
        "Update from base (git push)": "u",
        "Refresh from PR head (git push --pr)": "r",
        "Change base branch (git retarget)": "b",
        "Show diff (git diff)": "d",
    }
    assert _hotkeys(submenu("PR →")) == {"Open PR": "p", "Create/update PR": "P"}
    assert _hotkeys(submenu("Lifecycle →")) == {"Restart": "r", "Stop": "s", "Destroy": "D"}
    assert _hotkeys(submenu("Network →")) == {"Network: loose": "l", "Egress…": "e"}


def test_menu_hotkeys_on_a_stopped_row_keep_destroy_capital():
    menu = dashboard.MenuState("alpha-x", dashboard.menu_actions(_ctx(state="Stopped")))

    assert _hotkeys(menu) == {"Start": "s", "Network →": "w", "Destroy": "D"}


def test_menu_hotkeys_cover_the_repo_menu():
    menu = dashboard.RepoMenuState(
        "alpha",
        [
            ("New container…", "new"),
            ("New from PR…", "new-pr"),
            ("Credential group…", "credential-group"),
            ("Accounts…", "accounts"),
            dashboard.MenuGroup("Network →", (("Egress…", "net egress ls"),)),
            ("Apply config…", "apply"),
            dashboard.MenuGroup(
                "Diagnostics →", (("Doctor", "doctor"), ("Disk usage", "disk-usage"))
            ),
            ("Prune stale containers…", "prune"),
            ("Fold", "fold"),
        ],
    )

    assert _hotkeys(menu) == {
        "New container…": "n",
        "New from PR…": "p",
        "Credential group…": "c",
        "Accounts…": "a",
        "Network →": "w",
        "Apply config…": "y",
        "Diagnostics →": "d",
        "Prune stale containers…": "r",
        "Fold": "f",
    }


def _levels(menu: dashboard.MenuState | dashboard.RepoMenuState):
    """Every level of ``menu`` as (group label or None, entries)."""
    root = dashboard._menu_entries(menu)
    yield None, root
    for item in root:
        if isinstance(item, dashboard.MenuGroup):
            yield item.label, item.actions


def _assert_every_key_is_fixed(menu: dashboard.MenuState | dashboard.RepoMenuState) -> None:
    for group, entries in _levels(menu):
        if group == "Launch →":
            continue  # app labels come from the repo's config: the one dynamic level
        for item, key in zip(entries, dashboard.menu_hotkeys(entries), strict=True):
            preferred = dashboard._preferred_menu_key(item)
            assert preferred is not None, f"{item} has no fixed key"
            assert key == preferred, f"{item} lost {preferred!r} to a neighbour in {group}"


_LIFECYCLE_SUBSETS = (
    ("restart", "stop", "destroy"),
    ("restart",),
    ("stop",),
    ("destroy",),
    (),
)


@pytest.mark.parametrize("state", ["Running", "Stopped"])
@pytest.mark.parametrize("lifecycle", _LIFECYCLE_SUBSETS)
def test_container_menu_keys_never_move_whatever_else_is_shown(state, lifecycle):
    """Every entry keeps its own fixed key in every combination that can co-occur.

    Fails when a new menu entry has no `_MENU_KEYS` letter, or when two entries
    that can be on one level at once share one — either makes a key depend on
    which other entries happen to be visible.
    """
    autostart = (
        ("Autostart status", dashboard.dact.AUTOSTART_STATUS),
        ("Cancel autostart…", dashboard.dact.AUTOSTART_CANCEL),
    )
    before = (
        ("Snapshots…", dashboard.dact.SNAPSHOTS),
        ("Mount…", dashboard.dact.MOUNT_ADD),
        ("Unmount…", dashboard.dact.MOUNT_REMOVE),
    )
    for pr_number, pr_author, job, network, extras, pending in itertools.product(
        (None, 7),
        (False, True),
        ("none", "running", "failed"),
        ("strict", None),
        (False, True),
        (False, True),
    ):
        ctx = _ctx(
            state=state,
            pr_number=pr_number,
            pr_author=pr_author,
            has_job=job != "none",
            job_running=job == "running",
            job_clearable=job == "failed",
            current_network=network,
            apps=_apps("ide", "chrome"),
            git_status=dataclasses.replace(_dirty(), pending_pr_actions=2) if pending else None,
        )
        actions = [
            a
            for a in dashboard.menu_actions(ctx)
            if a[1] not in dashboard._CONTAINER_LIFECYCLE_VERBS or a[1] in lifecycle
        ]
        if extras:
            actions = dashboard._insert_after_job(actions, autostart)
            actions = dashboard._insert_before_network(actions, before)
        actions = dashboard._with_credential_group(actions)
        _assert_every_key_is_fixed(dashboard.MenuState("alpha-x", actions))


@pytest.mark.parametrize("drop", [None, "credential-group", "accounts", "apply", "prune"])
def test_repo_menu_keys_never_move_whatever_the_policy_hides(drop):
    actions: list[dashboard.MenuItem] = [
        ("New container…", "new"),
        ("New from PR…", "new-pr"),
        ("Credential group…", "credential-group"),
        ("Accounts…", "accounts"),
        dashboard.MenuGroup("Network →", (("Egress…", "net egress ls"),)),
        ("Apply config…", dashboard.dact.REPO_APPLY),
        dashboard.MenuGroup(
            dashboard.dact.DIAGNOSTICS_LABEL,
            (
                ("Doctor", dashboard.dact.REPO_DOCTOR),
                ("Disk usage", dashboard.dact.REPO_DISK_USAGE),
            ),
        ),
        ("Prune stale containers…", dashboard.dact.REPO_PRUNE),
        ("Fold", "fold"),
    ]
    menu = dashboard.RepoMenuState(
        "alpha", [a for a in actions if isinstance(a, dashboard.MenuGroup) or a[1] != drop]
    )
    _assert_every_key_is_fixed(menu)


def test_menu_hotkeys_fall_back_to_a_free_label_letter_then_digits():
    entries = [
        ("Attach tmux", "tmux"),
        ("Tally", "unknown-1"),  # preferred-free: t is taken, a is next
        ("Hook", "unknown-2"),  # h is the help key: never assigned, o follows
        ("ttt", "unknown-3"),  # every letter taken: the first digit
    ]

    assert dashboard.menu_hotkeys(entries) == ["t", "a", "o", "1"]


def test_menu_hotkeys_never_take_a_key_the_open_menu_already_handles():
    reserved = {"j", "k", "q", "h", "?", "S"}
    entries = [(f"{ch}{ch.upper()}", f"x-{ch}") for ch in "jkqhs"]

    keys = dashboard.menu_hotkeys(entries)

    assert not reserved & {k for k in keys if k}
    assert len({k for k in keys if k}) == len([k for k in keys if k])


def test_menu_hotkeys_preferred_keys_win_over_earlier_fallbacks():
    # "Tally" sits first, but `t` is Attach tmux's own key: the fallback yields.
    entries = [("Tally", "unknown"), ("Attach tmux", "tmux")]

    assert dashboard.menu_hotkeys(entries) == ["a", "t"]


def test_hotkey_menu_moves_the_cursor_to_the_entry_or_returns_none():
    root = _grouped_menu()  # Attach tmux, Git →, PR →

    hit = dashboard.hotkey_menu(root, b"p")
    assert hit is not None and hit.index == 2 and hit.active_group is None
    assert dashboard.hotkey_menu(root, b"z") is None
    assert dashboard.hotkey_menu(root, b"\x1b[A") is None


def test_render_menu_shows_each_entry_with_its_key():
    console = Console(width=60, record=True)
    console.print(dashboard._render_menu(_grouped_menu()))
    text = console.export_text()

    assert "[t] Attach tmux" in text
    assert "[g] Git →" in text
    assert "[p] PR →" in text


# --- RepoTarget: how a spawned `jailbee` child is pointed at one repo --------


def _dispatch_target(tmp_path, name="config.yaml"):
    """A configured repo rooted at ``tmp_path``, as the dispatch paths see it."""
    return dashboard.RepoTarget(tmp_path, tmp_path / name)


def test_repo_target_uses_config_flag_when_there_is_a_file(tmp_path):
    t = dashboard.RepoTarget(repo_root=tmp_path, config_path=tmp_path / ".jailbee" / "config.yaml")

    assert t.flags() == ["--config", str(tmp_path / ".jailbee" / "config.yaml")]
    assert t.cwd() == tmp_path


def test_repo_target_falls_back_to_cwd_for_a_scratch_repo(tmp_path):
    """There is no path to point `--config` at; the child resolves its config
    from the directory it runs in."""
    t = dashboard.RepoTarget(repo_root=tmp_path, config_path=None)

    assert t.flags() == []
    assert t.cwd() == tmp_path


def test_repo_target_of_reads_the_groups_root_and_config(tmp_path):
    group = dashboard.RepoGroup("alpha", str(tmp_path), tmp_path / "c.yaml", [])

    assert dashboard.RepoTarget.of(group) == _dispatch_target(tmp_path, "c.yaml")


def test_repo_target_of_accepts_a_scratch_group(tmp_path):
    """A repo with no config file is still a real repo with a real root, so it
    is addressable — that is the whole point of the cwd fallback."""
    target = dashboard.RepoTarget.of(dashboard.RepoGroup("alpha", str(tmp_path), None, []))

    assert target is not None
    assert target.flags() == []
    assert target.cwd() == tmp_path


def test_repo_target_of_is_none_for_an_orphan_group():
    """An orphan group has no repo root, so there is nothing to address at
    all: neither a `--config` path nor a directory to run the child in."""
    assert dashboard.RepoTarget.of(dashboard.RepoGroup("gamma", None, None, [])) is None


def test_dispatch_action_runs_jailbee_with_the_repos_config(mocker, tmp_path):
    config_path = tmp_path / "config.yaml"
    run = mocker.patch.object(dashboard.subprocess, "run")
    run.return_value.returncode = 0

    rc = dashboard._dispatch_action(_dispatch_target(tmp_path), "net loose", "alpha-x")

    run.assert_called_once_with(
        ["jailbee", "net", "loose", "alpha-x", "--config", str(config_path)],
        check=False,
        cwd=tmp_path,
    )
    assert rc == 0


def test_dispatch_action_forces_the_attach_verbs(mocker, tmp_path):
    """The JOB column already shows a failed background job, so the CLI's
    "continue anyway?" question would only ask what the operator just read.

    Covers every verb routed through the CLI's attach guard, not just the
    interactive two: `ide`/`chrome` would otherwise block on a prompt in
    whatever terminal the dashboard was started from.
    """
    config_path = tmp_path / "config.yaml"
    run = mocker.patch.object(dashboard.subprocess, "run")
    run.return_value.returncode = 0

    for verb in ("tmux", "shell", "ide", "chrome"):
        run.reset_mock()
        dashboard._dispatch_action(_dispatch_target(tmp_path), verb, "alpha-x")
        run.assert_called_once_with(
            ["jailbee", verb, "alpha-x", "--config", str(config_path), "--force"],
            check=False,
            cwd=tmp_path,
        )


def test_dispatch_action_rechecks_remote_policy_before_subprocess(mocker, tmp_path):
    from jailbee.config.models_remote import RemoteCommandPolicy, RemoteSSHConfig
    from jailbee.remote_ssh.router import RouteError

    run = mocker.patch.object(dashboard.subprocess, "run")
    policy = RemoteSSHConfig(commands=RemoteCommandPolicy(mode="allowlist", allow=["git merge"]))
    with pytest.raises(RouteError):
        dashboard._dispatch_action(
            _dispatch_target(tmp_path),
            "stop",
            "alpha-x",
            over_ssh=True,
            ssh_policy=policy,
        )
    run.assert_not_called()


def test_dispatch_action_does_not_force_other_verbs(mocker, tmp_path):
    """`--force` means different things per command (and most don't take it
    at all), so only the attach verbs get it appended."""
    run = mocker.patch.object(dashboard.subprocess, "run")
    run.return_value.returncode = 0

    dashboard._dispatch_action(_dispatch_target(tmp_path), "restart", "alpha-x")

    assert "--force" not in run.call_args[0][0]


def test_dispatch_action_routes_a_top_level_apps_container_through_the_flag(
    mocker, tmp_path, make_cfg
):
    """The bug this guards: `jailbee apps run <app> <container>` is *not*
    the same command as `jailbee apps run <app> --container <container>` —
    Ruling 24 made the container `apps run`'s `--container` option, not a
    second positional. A dispatch verb that put the container as a bare
    trailing positional would have it swallowed by the app's own variadic
    `args`, and the app would launch in the *default* container instead —
    silently, no error. This fails if `_app_menu_verb` ever goes back to
    dispatching a registry app by its bare name (`"figma"` instead of
    `"apps run figma --container"`), which is exactly what put the
    container in `args` in the first place."""
    from jailbee.apps import resolve_apps

    cfg = make_cfg(tmp_path, apps={"figma": {"command": "/f", "top_level": True}})
    spec = next(s for s in resolve_apps(cfg) if s.name == "figma")
    verb = dashboard._app_menu_verb(spec)

    config_path = tmp_path / "config.yaml"
    run = mocker.patch.object(dashboard.subprocess, "run")
    run.return_value.returncode = 0

    dashboard._dispatch_action(_dispatch_target(tmp_path), verb, "alpha-x")

    run.assert_called_once_with(
        [
            "jailbee",
            "apps",
            "run",
            "figma",
            "--container",
            "alpha-x",
            "--config",
            str(config_path),
            "--force",
        ],
        check=False,
        cwd=tmp_path,
    )


def test_dispatch_action_routes_a_non_top_level_app_to_a_real_command(mocker, tmp_path, make_cfg):
    """A non-`top_level` `apps:` entry has no `jailbee <app>` command at all
    — `entry._top_level_app_names` (and thus `entry.rewrite_app_argv`) only
    rewrites the bare name for an app that declared `top_level: true`.
    Dispatching through `apps run <app> --container` sidesteps that rewrite
    entirely, so a non-`top_level` app still reaches a real command instead
    of a Typer "no such command" error. Fails if the dispatch verb depended
    on `top_level` and fell back to the bare name for one that lacks it."""
    from jailbee.apps import resolve_apps

    cfg = make_cfg(tmp_path, apps={"figma": {"command": "/f"}})  # top_level defaults False
    spec = next(s for s in resolve_apps(cfg) if s.name == "figma")
    assert spec.top_level is False  # the config under test, not the fix

    verb = dashboard._app_menu_verb(spec)
    assert verb == "apps run figma --container"

    config_path = tmp_path / "config.yaml"
    run = mocker.patch.object(dashboard.subprocess, "run")
    run.return_value.returncode = 0

    dashboard._dispatch_action(_dispatch_target(tmp_path), verb, "alpha-x")

    run.assert_called_once_with(
        [
            "jailbee",
            "apps",
            "run",
            "figma",
            "--container",
            "alpha-x",
            "--config",
            str(config_path),
            "--force",
        ],
        check=False,
        cwd=tmp_path,
    )


def test_qtui_still_imports_the_constant():
    # qtui/actions.py derives _ASSUME_YES_VERBS from ATTACH_VERBS at import
    # time, before any Config exists — it must stay a plain module constant.
    assert {"shell", "tmux", "ide", "chrome", "firefox", "browser"} == dashboard.ATTACH_VERBS


def test_dispatch_action_reports_the_commands_exit_code(mocker, tmp_path):
    run = mocker.patch.object(dashboard.subprocess, "run")
    run.return_value.returncode = 2

    assert dashboard._dispatch_action(_dispatch_target(tmp_path), "tmux", "alpha-x") == 2


def test_dispatch_action_omits_the_config_flag_for_a_scratch_repo(mocker, tmp_path):
    """A scratch repo has no path to point `--config` at. It is addressed by
    running the child in the repo root instead, so the cwd is the only thing
    that says which repo this is — it must actually be set."""
    run = mocker.patch.object(dashboard.subprocess, "run")
    run.return_value.returncode = 0

    dashboard._dispatch_action(dashboard.RepoTarget(tmp_path, None), "net loose", "alpha-x")

    run.assert_called_once_with(["jailbee", "net", "loose", "alpha-x"], check=False, cwd=tmp_path)


def test_dispatch_action_pages_a_scratch_repo_from_its_repo_root(mocker, tmp_path):
    """The pager path spawns the command itself, so it needs the cwd too —
    otherwise `jailbee git diff` in a scratch repo resolves the dashboard's own
    directory instead of the row's."""
    mocker.patch.object(dashboard, "pager_argv", return_value=["less", "-R"])
    popen = mocker.patch.object(dashboard.subprocess, "Popen")
    popen.return_value.wait.return_value = 0
    # Patching `Popen` alone is process-wide and takes `subprocess.run` with
    # it too — mocked here (as the sibling non-scratch test already does) so
    # a regression that reaches the plain `run` fallback fails with a
    # readable assertion instead of a `TypeError` from the real subprocess API.
    run = mocker.patch.object(dashboard.subprocess, "run")

    dashboard._dispatch_action(dashboard.RepoTarget(tmp_path, None), "git diff", "alpha-x")

    producer = popen.call_args_list[0]
    assert producer.args[0] == ["jailbee", "git", "diff", "alpha-x", "--color"]
    assert producer.kwargs["cwd"] == tmp_path
    run.assert_not_called()  # the paged path replaces the plain run entirely


def test_dispatch_style_classifies_every_menu_verb():
    """`git diff` is long enough to want a pager; the other printing verbs get
    a keypress pause, because Live repaints over their output on return."""
    assert dashboard.dispatch_style("git diff") == "paged"
    for verb in ("pr", "git push", "git pull", "job log", "job log --follow"):
        assert dashboard.dispatch_style(verb) == "output", verb
    for verb in ("tmux", "shell", "ide", "chrome", "net loose", "restart", "destroy"):
        assert dashboard.dispatch_style(verb) == "plain", verb


def test_dispatch_style_leaves_pr_open_alone():
    """`pr --open` only opens a browser — pausing on it would be noise, and it
    is why the classification is exact rather than by leading token."""
    assert dashboard.dispatch_style("pr --open") == "plain"


def test_inline_noninteractive_commands_pause_for_output():
    assert dashboard.command_needs_pause("job ls")
    assert dashboard.command_needs_pause("ls")
    assert not dashboard.command_needs_pause("shell")


def test_every_printing_verb_is_a_real_menu_verb():
    """Guards against a typo in PRINTING_VERBS: a classified verb the menu never
    offers would silently never take its own code path."""
    offered = set()
    for state in ("Running", "Stopped", "Frozen"):
        offered |= {
            verb
            for _label, verb in dashboard.menu_actions(
                _ctx(
                    state=state,
                    apps=_apps("ide", "chrome"),
                    pr_number=7,
                    job_clearable=True,
                    has_job=True,
                )
            )
        }
        offered |= {
            verb
            for _label, verb in dashboard.menu_actions(
                _ctx(state=state, has_job=True, job_running=True)
            )
        }
    assert dashboard.PRINTING_VERBS <= offered, (
        f"unknown verbs: {dashboard.PRINTING_VERBS - offered}"
    )
    # The paged verbs are a subset, so the split cannot drop or invent one.
    assert dashboard._PAGED_VERBS <= dashboard.PRINTING_VERBS
    assert dashboard._OUTPUT_VERBS | dashboard._PAGED_VERBS == dashboard.PRINTING_VERBS


def test_pager_argv_prefers_the_environment(mocker):
    mocker.patch.dict(dashboard.os.environ, {"PAGER": "bat -p"}, clear=False)
    assert dashboard.pager_argv() == ["bat", "-p"]


def test_pager_argv_falls_back_to_less_then_more(mocker):
    mocker.patch.dict(dashboard.os.environ, {}, clear=True)
    which = mocker.patch.object(dashboard.shutil, "which", return_value=None)
    assert dashboard.pager_argv() is None

    which.side_effect = lambda n: "/usr/bin/more" if n == "more" else None
    assert dashboard.pager_argv() == ["more"]

    which.side_effect = lambda n: f"/usr/bin/{n}"
    assert dashboard.pager_argv() == ["less", "-R"]


def test_dispatch_action_pages_the_diff_and_forces_colour(mocker, tmp_path):
    config_path = tmp_path / "config.yaml"
    mocker.patch.object(dashboard, "pager_argv", return_value=["less", "-R"])
    popen = mocker.patch.object(dashboard.subprocess, "Popen")
    popen.return_value.wait.return_value = 0
    run = mocker.patch.object(dashboard.subprocess, "run")

    rc = dashboard._dispatch_action(_dispatch_target(tmp_path), "git diff", "alpha-x")

    assert rc == 0
    producer, viewer = popen.call_args_list
    assert producer.args[0] == [
        "jailbee",
        "git",
        "diff",
        "alpha-x",
        "--config",
        str(config_path),
        "--color",
    ]
    assert producer.kwargs["cwd"] == tmp_path
    assert viewer.args[0] == ["less", "-R"]
    # The pager is a plain viewer on a pipe: it has no repo of its own, so it
    # must not be pinned to one.
    assert "cwd" not in viewer.kwargs
    run.assert_not_called()  # the paged path replaces the plain run entirely


def test_remote_dispatch_action_never_starts_a_pager(mocker, tmp_path):
    """`less`'s `!`, `v` and `|` would run on the host. A remote diff is
    printed and paused on instead, and no pager process exists at all."""
    mocker.patch.object(dashboard, "pager_argv", return_value=["less", "-R"])
    popen = mocker.patch.object(dashboard.subprocess, "Popen")
    run = mocker.patch.object(dashboard.subprocess, "run")
    run.return_value.returncode = 0
    wait = mocker.patch.object(dashboard, "_wait_for_return")

    rc = dashboard._dispatch_action(_dispatch_target(tmp_path), "git diff", "alpha-x", remote=True)

    assert rc == 0
    popen.assert_not_called()
    assert run.call_args.args[0][:4] == ["jailbee", "git", "diff", "alpha-x"]
    wait.assert_called_once_with()


def test_dispatch_action_pauses_after_a_printing_verb(mocker, tmp_path):
    run = mocker.patch.object(dashboard.subprocess, "run")
    run.return_value.returncode = 0
    wait = mocker.patch.object(dashboard, "_wait_for_return")

    dashboard._dispatch_action(_dispatch_target(tmp_path), "git push", "alpha-x")

    wait.assert_called_once_with()


def test_dispatch_action_pauses_after_merge(mocker, tmp_path):
    run = mocker.patch.object(dashboard.subprocess, "run")
    run.return_value.returncode = 0
    wait = mocker.patch.object(dashboard, "_wait_for_return")

    dashboard._dispatch_action(_dispatch_target(tmp_path), "merge", "alpha-x")

    assert run.call_args.args[0][:3] == ["jailbee", "merge", "alpha-x"]
    wait.assert_called_once_with()


def test_dispatch_action_does_not_pause_after_an_interactive_verb(mocker, tmp_path):
    """tmux and shell end when the user leaves them; there is nothing left to
    read, and an extra keypress would just be in the way."""
    run = mocker.patch.object(dashboard.subprocess, "run")
    run.return_value.returncode = 0
    wait = mocker.patch.object(dashboard, "_wait_for_return")

    dashboard._dispatch_action(_dispatch_target(tmp_path), "tmux", "alpha-x")

    wait.assert_not_called()


def test_dispatch_action_falls_back_to_a_pause_when_there_is_no_pager(mocker, tmp_path):
    mocker.patch.object(dashboard, "pager_argv", return_value=None)
    run = mocker.patch.object(dashboard.subprocess, "run")
    run.return_value.returncode = 3
    wait = mocker.patch.object(dashboard, "_wait_for_return")

    rc = dashboard._dispatch_action(_dispatch_target(tmp_path), "git diff", "alpha-x")

    assert rc == 3
    wait.assert_called_once_with()
    assert "--color" not in run.call_args.args[0]  # no pager, so no forced colour


def test_dispatch_action_falls_back_when_the_pager_cannot_be_spawned(mocker, tmp_path):
    """`which` said yes and `exec` said no. The command still has to run, and
    its output still has to be readable."""
    mocker.patch.object(dashboard, "pager_argv", return_value=["less", "-R"])
    producer = mocker.MagicMock()
    popen = mocker.patch.object(dashboard.subprocess, "Popen")
    popen.side_effect = [producer, OSError("no less")]
    run = mocker.patch.object(dashboard.subprocess, "run")
    run.return_value.returncode = 0
    wait = mocker.patch.object(dashboard, "_wait_for_return")

    rc = dashboard._dispatch_action(_dispatch_target(tmp_path), "git diff", "alpha-x")

    assert rc == 0
    # Nothing will ever read the pipe, so the first process must not be left
    # blocked on a full one.
    producer.kill.assert_called_once_with()
    run.assert_called_once()
    wait.assert_called_once_with()


def test_dispatch_action_does_not_mistake_a_vanished_repo_for_a_missing_pager(mocker, tmp_path):
    """The producer itself can fail to start too — most notably when the
    repo's directory has disappeared out from under the dispatch. That must
    not be logged (or handled) as "pager failed": it has to surface as a bare
    `OSError` so the caller (`run`'s `dispatch`) can tell the two apart and
    report the real cause. Before the fix, this was swallowed by the same
    `except OSError` that exists for a missing pager and then fell through to
    the plain `subprocess.run` fallback — which would raise the identical
    `OSError` uncaught, taking the whole TUI down.
    """
    mocker.patch.object(dashboard, "pager_argv", return_value=["less", "-R"])
    popen = mocker.patch.object(dashboard.subprocess, "Popen")
    popen.side_effect = OSError("no such directory")
    run = mocker.patch.object(dashboard.subprocess, "run")

    with pytest.raises(OSError):
        dashboard._dispatch_action(_dispatch_target(tmp_path), "git diff", "alpha-x")

    run.assert_not_called()  # must not fall back to a doomed retry


def test_dispatch_falls_back_to_a_pager_unavailable_when_the_pager_itself_fails(mocker, tmp_path):
    """Sanity check alongside the test above: the pager-missing case is still
    a `_PagerUnavailableError` (an `OSError` subclass), and `_run_paged` raising it
    is what the call site's `except _PagerUnavailableError` actually catches."""
    mocker.patch.object(dashboard, "pager_argv", return_value=["less", "-R"])
    producer = mocker.MagicMock()
    popen = mocker.patch.object(dashboard.subprocess, "Popen")
    popen.side_effect = [producer, OSError("no less")]

    with pytest.raises(dashboard._PagerUnavailableError):
        dashboard._run_paged(["jailbee", "git", "diff"], ["less", "-R"], tmp_path)


def test_actions_for_container_matches_menu_actions():
    from pathlib import Path

    from jailbee.dashboard import (
        RepoGroup,
        actions_for_container,
        menu_actions,
    )
    from jailbee.lifecycle import ContainerInfo

    running = ContainerInfo(
        name="p-foo",
        state="Running",
        network="strict",
        ip="1.2.3.4",
        memory_limit="2GB",
        repo="p",
    )
    groups = [
        RepoGroup(
            "p",
            "/repo",
            Path("/repo/.jailbee/config.yaml"),
            [running],
            apps=_apps("ide"),
        )
    ]
    expected = menu_actions(_ctx(apps=_apps("ide")))
    assert actions_for_container(groups, "p-foo") == expected
    assert actions_for_container(groups, "nope") == []
    assert actions_for_container(groups, None) == []


def test_gather_rows_sets_apps_from_config(tmp_path, mocker, make_cfg):
    """Covers registry order, the label fallback, and the dispatch verb in
    one pass: `ide` (a builtin, `jetbrains.enabled`) carries a real
    description ("JetBrains idea") and dispatches by its bare name — a real
    top-level `jailbee` command. `figma` (a bare `apps:` entry with no
    `description` and no `top_level` set) falls back to its own name as the
    label, but its dispatch verb is `"apps run figma --container"`, not the
    bare name: a bare `jailbee figma <container>` either fails outright (no
    such command, since it never declared `top_level`) or — worse — for one
    that did, gets rewritten to `apps run figma <container>`, where the
    container lands in the app's own variadic `args` instead of naming a
    container (Ruling 24 made the container `apps run`'s `--container`
    option, not a second positional). `apps:` sorts before `jetbrains`
    alphabetically, so if `gather_rows` read YAML/dict key order instead of
    delegating to `resolve_apps`, `figma` would come first here."""
    cfg = make_cfg(
        tmp_path / "alpha",
        jetbrains={"enabled": True},
        chrome={"enabled": False},
        apps={"figma": {"command": "/f"}},
    )
    root = tmp_path / "alpha"
    mocker.patch.object(dashboard, "load_repo_config", return_value=cfg)

    def fake_list(c, incus, *, all_repos, with_git_status, with_background, instances):
        return [] if all_repos else [_ci("alpha-one", "alpha")]

    mocker.patch.object(dashboard, "list_containers", side_effect=fake_list)
    groups = dashboard.gather_rows(mocker.MagicMock(), [root], with_git=False)
    group = next(g for g in groups if g.prefix == "alpha")
    assert group.apps == [
        dashboard.AppMenuEntry("ide", "JetBrains idea"),
        dashboard.AppMenuEntry("apps run figma --container", "figma"),
    ]


def test_gather_rows_orphan_groups_have_no_apps(tmp_path, mocker, make_cfg):
    cfg = make_cfg(tmp_path / "alpha")
    root = tmp_path / "alpha"
    mocker.patch.object(dashboard, "load_repo_config", return_value=cfg)

    def fake_list(c, incus, *, all_repos, with_git_status, with_background, instances):
        if all_repos:
            return [_ci("alpha-one", "alpha"), _ci("gamma-x", "gamma")]
        return [_ci("alpha-one", "alpha")]

    mocker.patch.object(dashboard, "list_containers", side_effect=fake_list)
    groups = dashboard.gather_rows(mocker.MagicMock(), [root], with_git=False)
    orphan = next(g for g in groups if g.prefix == "gamma")
    assert orphan.apps == []


def test_visible_fields_excludes_hidden_and_respects_default_table():
    from datetime import datetime

    c = ContainerInfo(
        name="p-foo", state="Running", network="strict", ip=None, memory_limit="2GB", repo="p"
    )
    names = [f.name for f in dashboard.visible_fields(datetime.now().astimezone(), [c])]

    # Hidden columns never appear.
    assert "repo" not in names
    assert "full_name" not in names
    assert "git_status" not in names
    assert "created" not in names
    assert "ttl" not in names  # folded into the NETWORK cell instead
    # Core columns do.
    assert "name" in names
    assert "state" in names
    assert "network" in names


def test_dashboard_keeps_mem_that_ls_drops_and_ip_is_off_in_both():
    """MEM is the one deliberate difference between the two default sets.

    MEM is a live sample: it earns its width in a view that refreshes and not
    in a one-shot listing. IP is off in both — `jailbee apply` writes
    /etc/hosts entries, so the address is rarely how a container is reached,
    and the dashboards used to pay 15 columns for it. Both stay reachable:
    IP via the settings UI or `ls --fields ip`, MEM via `ls --fields mem`.
    """
    from datetime import UTC, datetime

    from jailbee.lifecycle import ls_field_specs

    c = ContainerInfo(
        name="p-foo", state="Running", network="strict", ip="10.0.0.5", memory_limit="2GB", repo="p"
    )
    now = datetime(2026, 6, 8, 12, 0, tzinfo=UTC)

    dashboard_names = [f.name for f in dashboard.visible_fields(now, [c])]
    ls_names = [f.name for f in ls_field_specs(now=now, all_repos=False) if f.default_table]

    assert "mem" in dashboard_names and "mem" not in ls_names
    assert "ip" not in dashboard_names and "ip" not in ls_names


def test_visible_fields_network_cell_shows_mode_icon_only():
    now = datetime(2026, 6, 8, 12, 0, tzinfo=UTC)
    loose = ContainerInfo(
        name="p-loose",
        state="Running",
        network="loose",
        ip=None,
        memory_limit=None,
        repo="p",
        loose_until=now + timedelta(minutes=12, seconds=30),
    )
    strict = ContainerInfo(
        name="p-strict", state="Running", network="strict", ip=None, memory_limit=None, repo="p"
    )
    fields = dashboard.visible_fields(now, [loose, strict])
    network_field = next(f for f in fields if f.name == "network")
    assert network_field.cell(loose) == "○"
    assert network_field.cell(strict) == "●"


def test_network_cell_is_icon_only_for_a_long_ttl():
    from datetime import UTC, datetime, timedelta

    from jailbee.lifecycle import ContainerInfo

    now = datetime(2026, 5, 20, 12, 0, 0, tzinfo=UTC)
    loose = ContainerInfo(
        name="myrepo-feat-x",
        state="Running",
        network="loose",
        ip=None,
        memory_limit=None,
        loose_until=now + timedelta(hours=2, minutes=5),
    )
    network_field = next(f for f in dashboard.visible_fields(now, [loose]) if f.name == "network")
    assert network_field.cell(loose) == "○"


def test_visible_fields_network_cell_unknown_loose_until():
    now = datetime(2026, 6, 8, 12, 0, tzinfo=UTC)
    c = ContainerInfo(
        name="p-loose",
        state="Running",
        network="loose",
        ip=None,
        memory_limit=None,
        repo="p",
        loose_until=None,
    )
    fields = dashboard.visible_fields(now, [c])
    network_field = next(f for f in fields if f.name == "network")
    assert network_field.cell(c) == "○"


def test_visible_fields_includes_pr_when_a_container_has_one():
    from datetime import datetime

    now = datetime.now().astimezone()
    with_pr = ContainerInfo(
        name="p-foo",
        state="Running",
        network="strict",
        ip=None,
        memory_limit=None,
        repo="p",
        pr_number=7,
        pr_author=True,
    )
    names = [f.name for f in dashboard.visible_fields(now, [with_pr])]
    assert "pr" in names


def test_rendered_columns_do_not_follow_conditional_cell_presence():
    now = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)
    plain = dashboard.RepoGroup("alpha", "/a", None, [_ci("alpha-x", "alpha")])
    with_pr = dataclasses.replace(
        plain,
        containers=[dataclasses.replace(plain.containers[0], pr_number=42)],
    )
    for width in (56, 80, 120):
        rendered = [
            _render_text(
                dashboard.render(
                    [group],
                    None,
                    now=now,
                    git_enabled=True,
                    enabled=("name", "pr", "state"),
                    shown_columns=("name", "pr", "state"),
                ),
                width=width,
            )
            for group in (plain, with_pr)
        ]
        headers = [next(line for line in text.splitlines() if "NAME" in line) for text in rendered]
        assert headers[0] == headers[1], width
        assert "PR" in headers[0], width


def test_rendered_columns_do_not_reflow_when_live_text_changes():
    now = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)
    plain = dashboard.RepoGroup("alpha", "/a", None, [_ci("alpha-x", "alpha")])
    active = dataclasses.replace(
        plain,
        containers=[dataclasses.replace(plain.containers[0], name="alpha-" + "x" * 36)],
    )
    for width in (36, 56, 80, 120):
        rendered = [
            _render_text(
                dashboard.render(
                    [group],
                    None,
                    now=now,
                    git_enabled=True,
                    enabled=("name", "state", "network", "created", "pr"),
                ),
                width=width,
            )
            for group in (plain, active)
        ]
        headers = [next(line for line in text.splitlines() if "NAME" in line) for text in rendered]
        assert headers[0] == headers[1], width


def test_budgeted_base_value_stays_on_one_row():
    now = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)
    container = _ci("alpha-x", "alpha")
    frames = []
    for base in ("main", "feature/long-branch (tracking)"):
        group = dashboard.RepoGroup(
            "alpha", "/a", None, [dataclasses.replace(container, base_branch=base)]
        )
        frames.append(
            _render_text(
                dashboard.render(
                    [group], None, now=now, git_enabled=True, enabled=("name", "base", "state")
                ),
                width=80,
            ).splitlines()
        )
    assert len(frames[0]) == len(frames[1])
    assert any("…" in line and "▶" in line for line in frames[1])


def test_visible_fields_omits_pr_when_no_container_has_one():
    from datetime import datetime

    now = datetime.now().astimezone()
    no_pr = ContainerInfo(
        name="p-foo", state="Running", network="strict", ip=None, memory_limit=None, repo="p"
    )
    names = [f.name for f in dashboard.visible_fields(now, [no_pr])]
    assert "pr" not in names


def test_visible_fields_defaults_to_todays_hidden_set():
    """Omitting `columns` must render exactly what the dashboard renders now."""
    from jailbee.config import DASHBOARD_DEFAULT_HIDE
    from jailbee.lifecycle import ContainerInfo

    c = ContainerInfo(name="p-foo", state="Running", network="strict", ip=None, memory_limit=None)
    names = [f.name for f in dashboard.visible_fields(datetime.now().astimezone(), [c])]

    assert not set(names) & set(DASHBOARD_DEFAULT_HIDE)
    assert "name" in names


def test_visible_fields_enabled_set_can_drop_a_dashboard_only_column():
    """An enabled set is authoritative in both directions: it can drop `mem`,
    which is on by default in the dashboards."""
    from datetime import UTC, datetime

    c = ContainerInfo(
        name="p-foo", state="Running", network="strict", ip="10.0.0.5", memory_limit="2GB", repo="p"
    )
    now = datetime(2026, 6, 8, 12, 0, tzinfo=UTC)

    names = [f.name for f in dashboard.visible_fields(now, [c], ["name", "state"])]
    assert names == ["name", "state"]


def test_visible_fields_enabled_set_can_add_an_off_by_default_column():
    """...and it can add one that is off by default everywhere, which a `hide`
    list never could."""
    from datetime import UTC, datetime

    c = ContainerInfo(
        name="p-foo", state="Running", network="strict", ip=None, memory_limit="2GB", repo="p"
    )
    now = datetime(2026, 6, 8, 12, 0, tzinfo=UTC)

    names = [f.name for f in dashboard.visible_fields(now, [c], ["name", "memory_limit"])]
    assert names == ["name", "memory_limit"]


def test_visible_fields_renders_in_canonical_order_not_stored_order():
    """Stored order is not significant: the dashboards iterate the field-spec
    list and filter by membership. Column reordering is a separate feature,
    and this keeps a stored list from half-implementing it."""
    from datetime import UTC, datetime

    c = ContainerInfo(name="p-foo", state="Running", network="strict", ip=None, memory_limit=None)
    now = datetime(2026, 6, 8, 12, 0, tzinfo=UTC)

    names = [f.name for f in dashboard.visible_fields(now, [c], ["state", "name"])]
    assert names == ["name", "state"]


def test_visible_fields_still_applies_show_if_to_an_enabled_column():
    """The deliberate difference from `ls --fields`, where naming a column
    clears its `show_if`. Here enabling PR means "show it when a container
    tracks one", not "show an empty PR column forever" — the settings UI says
    so on the row itself. Without this, four dynamic columns (`job`, `ttl`,
    `pr`, `mode`) would render permanently empty for anyone who ticked them.
    """
    from datetime import UTC, datetime

    no_pr = ContainerInfo(
        name="p-foo", state="Running", network="strict", ip=None, memory_limit=None
    )
    now = datetime(2026, 6, 8, 12, 0, tzinfo=UTC)

    names = [f.name for f in dashboard.visible_fields(now, [no_pr], ["name", "pr"])]
    assert names == ["name"]

    with_pr = ContainerInfo(
        name="p-bar", state="Running", network="strict", ip=None, memory_limit=None, pr_number=7
    )
    names = [f.name for f in dashboard.visible_fields(now, [with_pr], ["name", "pr"])]
    assert names == ["name", "pr"]


def test_visible_fields_unknown_enabled_name_is_ignored():
    """A name that is no longer a real column (a removed field, a hand-edited
    row) is skipped rather than raising — same principle as the tolerant
    decode in db/view_prefs."""
    from datetime import UTC, datetime

    c = ContainerInfo(name="p-foo", state="Running", network="strict", ip=None, memory_limit=None)
    now = datetime(2026, 6, 8, 12, 0, tzinfo=UTC)

    names = [f.name for f in dashboard.visible_fields(now, [c], ["name", "gone", "state"])]
    assert names == ["name", "state"]


def test_default_columns_matches_the_built_in_dashboard_set():
    from jailbee.config import DASHBOARD_DEFAULT_HIDE

    names = dashboard.default_columns()
    assert "name" in names
    assert "mem" in names  # the dashboard-only default
    assert "agent_compact" in names
    assert "agent" not in names
    assert "ip" not in names  # Task 1
    assert not set(names) & set(DASHBOARD_DEFAULT_HIDE)


def test_enabled_from_column_config_reproduces_a_legacy_hide_block():
    """The seed path: a `dashboard:` block resolves to the exact set it used
    to render, so nobody's columns change on upgrade."""
    from jailbee.config import ColumnConfig

    names = dashboard.enabled_from_column_config(ColumnConfig(hide=["mem", "state"]))
    assert "mem" not in names
    assert "state" not in names
    assert "name" in names
    # `hide` replaced the built-in list rather than extending it, so a column
    # the default hid is back — the legacy semantics, preserved by the seed.
    assert "created" in names


def test_enabled_from_column_config_reproduces_a_legacy_fields_block():
    from jailbee.config import ColumnConfig

    names = dashboard.enabled_from_column_config(ColumnConfig(fields=["name", "created"]))
    assert names == ("name", "created")


def test_explicit_network_field_list_shows_mode_icon_only():
    """An explicit field list does not add TTL to the network icon."""
    from datetime import timedelta

    from jailbee.lifecycle import ContainerInfo

    now = datetime.now().astimezone()
    loose = ContainerInfo(
        name="p-foo",
        state="Running",
        network="loose",
        ip=None,
        memory_limit=None,
        loose_until=now + timedelta(hours=2),
    )

    fields = dashboard.visible_fields(now, [loose], ["name", "network"])
    network = next(f for f in fields if f.name == "network")

    assert network.cell(loose) == "○"


def test_global_config_or_defaults_gets_the_sanitized_block_not_the_default(tmp_path, monkeypatch):
    """A typo in the global `dashboard:` block must not lose the whole block —
    the dashboard used to swallow `load_global_config`'s `ConfigError` and
    degrade to `GlobalConfig()` entirely. Now `load_global_config` recovers
    from the typo itself, so the dashboard sees the sanitized block (valid
    names kept) rather than the built-in default."""
    xdg = tmp_path / ".config"
    (xdg / "jailbee").mkdir(parents=True)
    (xdg / "jailbee" / "global.yaml").write_text("dashboard:\n  fields: [name, nosuchfield]\n")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))

    gcfg = dashboard.global_config_or_defaults()

    assert gcfg.dashboard.fields == ["name"]


def test_seed_view_state_imports_the_global_dashboard_block_once(mocker):
    """Nobody's columns change on upgrade: the deprecated global block is
    resolved once into the front-end's row."""
    from sqlmodel import SQLModel, create_engine

    from jailbee.db.view_prefs import FRONTEND_TUI, load_view_state
    from jailbee.global_config import GlobalConfig

    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    gcfg = GlobalConfig(dashboard={"fields": ["name", "state"]})
    mocker.patch.object(dashboard, "load_global_config", return_value=(gcfg, []))

    state = dashboard.seed_view_state(engine, FRONTEND_TUI)

    assert state.columns == ("name", "state")
    assert load_view_state(engine, FRONTEND_TUI).columns == ("name", "state")


def test_seed_view_state_leaves_an_existing_row_alone(mocker):
    """Seeding happens once. After that the YAML block is inert — editing it
    must not reach back into a front-end the user has since configured."""
    from sqlmodel import SQLModel, create_engine

    from jailbee.db.view_prefs import FRONTEND_TUI, ViewState, save_view_state
    from jailbee.global_config import GlobalConfig

    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    save_view_state(engine, FRONTEND_TUI, ViewState(columns=("name",)))
    gcfg = GlobalConfig(dashboard={"fields": ["name", "state"]})
    mocker.patch.object(dashboard, "load_global_config", return_value=(gcfg, []))

    state = dashboard.seed_view_state(engine, FRONTEND_TUI)

    assert state.columns == ("name",)


def test_seed_view_state_ignores_a_repo_level_block(mocker):
    """The seeded value is a personal, cross-repo setting, so it must come
    from the global layer only — never the repo layer, even when one exists
    and disagrees with it. Seeding from whichever repo the user happened to
    launch from first would let one repo silently define their view
    everywhere.

    The global and (mocked) repo layers are given *different* `dashboard:`
    blocks on purpose: if `seed_view_state` ever started consulting the
    repo layer, the result would flip to the repo's columns and
    `load_repo_config` would stop being uncalled. The previous version of this
    test had no repo config to differ against, so it passed identically
    against an implementation that *did* consult one — it only pinned
    "default global config -> default columns", never the "repo is
    ignored" claim in its own name.
    """
    from sqlmodel import SQLModel, create_engine

    from jailbee.db.view_prefs import FRONTEND_TUI
    from jailbee.global_config import GlobalConfig

    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    gcfg = GlobalConfig(dashboard=dashboard.ColumnConfig(fields=["name", "state"]))
    mocker.patch.object(dashboard, "load_global_config", return_value=(gcfg, []))
    repo_cfg = mocker.Mock(dashboard=dashboard.ColumnConfig(fields=["ip", "mem"]))
    load_repo_config = mocker.patch.object(dashboard, "load_repo_config", return_value=repo_cfg)

    state = dashboard.seed_view_state(engine, FRONTEND_TUI)

    assert state.columns == ("name", "state")  # the global block's answer, not the repo's
    load_repo_config.assert_not_called()  # the repo layer is never even read


def test_seed_view_state_seeds_the_two_frontends_independently(mocker):
    from sqlmodel import SQLModel, create_engine

    from jailbee.db.view_prefs import FRONTEND_QT, FRONTEND_TUI, ViewState, save_view_state
    from jailbee.global_config import GlobalConfig

    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    gcfg = GlobalConfig(dashboard={"fields": ["name", "state"]})
    mocker.patch.object(dashboard, "load_global_config", return_value=(gcfg, []))
    save_view_state(engine, FRONTEND_TUI, ViewState(columns=("name",)))

    assert dashboard.seed_view_state(engine, FRONTEND_TUI).columns == ("name",)
    assert dashboard.seed_view_state(engine, FRONTEND_QT).columns == ("name", "state")


def test_seed_view_state_filters_a_stale_column_name(mocker):
    """A column that has since been renamed or removed must not survive into
    the returned state — an all-phantom set would otherwise be able to reach
    the front-ends' last-column guards without those guards ever firing
    (the stored length is nonzero, but nothing real is left after both the
    TUI's and the Qt window's own filtering skip the unknown name)."""
    from sqlmodel import SQLModel, create_engine

    from jailbee.db.view_prefs import FRONTEND_TUI, ViewState, save_view_state
    from jailbee.global_config import GlobalConfig

    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    save_view_state(engine, FRONTEND_TUI, ViewState(columns=("name", "old_removed_col")))
    mocker.patch.object(dashboard, "load_global_config", return_value=(GlobalConfig(), []))

    state = dashboard.seed_view_state(engine, FRONTEND_TUI)

    assert state.columns == ("name",)


def test_seed_view_state_migrates_retired_diff_with_visible_notice(mocker):
    from sqlmodel import SQLModel, create_engine

    from jailbee.db.view_prefs import FRONTEND_TUI, ViewState, save_view_state

    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    save_view_state(engine, FRONTEND_TUI, ViewState(columns=("name", "ahead_diff")))
    notice = dashboard.stored_column_migration_notice(("name", "ahead_diff"))
    assert notice is not None and "ahead_diff" in notice and "target_diff" in notice
    assert "--incoming" in notice
    shown: list[str] = []
    state = dashboard.seed_view_state(engine, FRONTEND_TUI, on_migration=shown.append)
    assert state.columns == ("name", "target_diff")
    assert shown == [notice]


def test_seed_view_state_persists_retired_diff_migration_so_notice_shows_once(mocker):
    from sqlmodel import SQLModel, create_engine

    from jailbee.db.view_prefs import FRONTEND_QT, ViewState, load_view_state, save_view_state

    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    folded = frozenset({"p"})
    save_view_state(engine, FRONTEND_QT, ViewState(columns=("name", "ahead_diff"), folded=folded))

    first: list[str] = []
    dashboard.seed_view_state(engine, FRONTEND_QT, on_migration=first.append)
    stored = load_view_state(engine, FRONTEND_QT)
    assert stored.columns == ("name", "target_diff")
    assert stored.folded == folded

    second: list[str] = []
    state = dashboard.seed_view_state(engine, FRONTEND_QT, on_migration=second.append)
    assert len(first) == 1
    assert second == []
    assert state.columns == ("name", "target_diff")


def test_dashboard_config_migration_notice_is_visible_not_debug_only(mocker):
    mocker.patch.object(
        dashboard,
        "load_global_config",
        return_value=(
            dashboard.GlobalConfig(),
            ["dashboard.fields: 'ahead_diff' retired; use 'target_diff'"],
        ),
    )
    notice = dashboard.dashboard_config_migration_notice()
    assert notice is not None and "ahead_diff" in notice and "target_diff" in notice


def test_dashboard_repo_migration_notice_uses_gathered_config_without_new_reads():
    group = dashboard.RepoGroup(
        "p", "/repo", None, [], column_notice="ls.fields: 'ahead_diff' retired; use 'target_diff'"
    )
    notices = dashboard.dashboard_group_notices([group])
    assert len(notices) == 1 and "p" in notices[0] and "target_diff" in notices[0]


def test_seed_view_state_renames_a_stored_alias_rather_than_dropping_it(mocker):
    """`claude_group` was renamed `group` in this release. A user who had the
    column on has the old name in `view_prefs`, which `all_column_names` no
    longer knows — and per `seed_view_state`'s own contract the next save of
    any kind drops an unknown name for good. So the rename has to happen
    before the filter, or the column is lost permanently rather than carried
    over."""
    from sqlmodel import SQLModel, create_engine

    from jailbee.db.view_prefs import FRONTEND_TUI, ViewState, save_view_state
    from jailbee.global_config import GlobalConfig

    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    save_view_state(engine, FRONTEND_TUI, ViewState(columns=("name", "claude_group")))
    mocker.patch.object(dashboard, "load_global_config", return_value=(GlobalConfig(), []))

    state = dashboard.seed_view_state(engine, FRONTEND_TUI)

    assert state.columns == ("name", "group")


def test_seed_view_state_does_not_duplicate_a_column_both_spellings_name(mocker):
    """A stored set holding the old and the new name collapses to one column:
    the rename makes them the same column, and a duplicate would inflate the
    front-ends' last-column count exactly as a phantom name does."""
    from sqlmodel import SQLModel, create_engine

    from jailbee.db.view_prefs import FRONTEND_TUI, ViewState, save_view_state
    from jailbee.global_config import GlobalConfig

    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    save_view_state(engine, FRONTEND_TUI, ViewState(columns=("group", "name", "claude_group")))
    mocker.patch.object(dashboard, "load_global_config", return_value=(GlobalConfig(), []))

    state = dashboard.seed_view_state(engine, FRONTEND_TUI)

    assert state.columns == ("group", "name")


def test_seed_view_state_falls_back_to_default_when_every_stored_name_is_stale(mocker):
    """The empty-after-filtering case: if nothing in the stored set is a real
    column any more, the built-in default set is used instead of an empty
    tuple — the same "never zero columns" invariant the menu guard enforces
    at the other end."""
    from sqlmodel import SQLModel, create_engine

    from jailbee.db.view_prefs import FRONTEND_TUI, ViewState, save_view_state
    from jailbee.global_config import GlobalConfig

    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    save_view_state(engine, FRONTEND_TUI, ViewState(columns=("old_removed_col",)))
    mocker.patch.object(dashboard, "load_global_config", return_value=(GlobalConfig(), []))

    state = dashboard.seed_view_state(engine, FRONTEND_TUI)

    assert state.columns == dashboard.default_columns()


def test_seed_view_state_does_not_rewrite_the_stored_row(mocker):
    """`seed_view_state` never writes for an unknown name (only the retired
    `ahead_diff` rename is persisted): filtering happens only on the
    value it returns, not on the stored row, which still has the phantom
    name right after this call.

    That is narrower than "the name survives the session" — it does not,
    in general. Both front-ends hold the filtered value as their long-lived
    `enabled` / `_enabled_columns`, and the next save triggered by *any*
    action (e.g. folding a repo group) writes that filtered value back,
    dropping the phantom from storage for good. This test only pins down
    that this one function is not that save."""
    from sqlmodel import SQLModel, create_engine

    from jailbee.db.view_prefs import FRONTEND_TUI, ViewState, load_view_state, save_view_state
    from jailbee.global_config import GlobalConfig

    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    save_view_state(engine, FRONTEND_TUI, ViewState(columns=("name", "old_removed_col")))
    mocker.patch.object(dashboard, "load_global_config", return_value=(GlobalConfig(), []))

    dashboard.seed_view_state(engine, FRONTEND_TUI)

    assert load_view_state(engine, FRONTEND_TUI).columns == ("name", "old_removed_col")


def _render_text(renderable: RenderableType, width: int = 200) -> str:
    console = Console(record=True, width=width)
    console.print(renderable)
    return console.export_text()


def _render_ansi_lines(renderable: RenderableType, width: int = 200) -> list[str]:
    """The frame's lines with their ANSI styling, for asserting on highlights."""
    # `no_color=False` overrides the suite's NO_COLOR, which would strip the
    # very colour these assertions look for.
    console = Console(
        record=True, width=width, force_terminal=True, color_system="standard", no_color=False
    )
    console.print(renderable)
    return console.export_text(styles=True).splitlines()


def _cursor_lines(lines: list[str]) -> list[str]:
    """Lines carrying the cursor highlight — the only cursor indicator."""
    console = Console(force_terminal=True, color_system="standard", no_color=False)
    with console.capture() as cap:
        console.print(f"[{dashboard.CURSOR_STYLE}]x[/]", end="")
    sgr = cap.get().split("x", 1)[0]
    return [ln for ln in lines if sgr in ln]


def _screen_lines(groups, overlay, height: int) -> list[str]:
    """One frame as the full-screen dashboard draws it at ``height`` rows."""
    frame = dashboard.render(
        groups,
        dashboard.Row("container", groups[0].containers[0].name),
        now=datetime(2026, 6, 8, 12, 0, tzinfo=UTC),
        git_enabled=True,
        overlay=overlay,
        height=height,
    )
    return _render_text(frame, width=100).splitlines()


def _tall_group(tmp_path, n: int) -> dashboard.RepoGroup:
    return dashboard.RepoGroup(
        "alpha", "/repos/alpha", tmp_path / "a.yaml", [_ci(f"alpha-{i}", "alpha") for i in range(n)]
    )


def test_render_scrolls_a_menu_taller_than_the_screen_to_its_cursor(tmp_path):
    menu = dashboard.MenuState("alpha-0", [(f"Action {i}", f"v{i}") for i in range(30)], index=25)
    lines = _screen_lines([_tall_group(tmp_path, 3)], menu, height=20)
    text = "\n".join(lines)
    assert len(lines) <= 20
    assert re.search(r"▸ (\[\w\]|   ) Action 25\b", text)  # keys run out before 25
    assert "Action 0 " not in text
    assert "more" in text
    assert lines[-1].startswith("╰")  # the frame's bottom border is on screen


def test_render_scrolls_a_picker_taller_than_the_screen_to_its_cursor(tmp_path):
    entries = tuple(dashboard.PickerEntry(f"Entry {i}", str(i)) for i in range(30))
    picker = dashboard.Picker("x", "Pick one", entries, index=29)
    lines = _screen_lines([_tall_group(tmp_path, 3)], picker, height=20)
    assert len(lines) <= 20
    assert "▸ Entry 29" in "\n".join(lines)


def test_render_cuts_a_table_taller_than_the_screen_to_keep_the_menu_visible(tmp_path):
    menu = dashboard.MenuState("alpha-0", [(f"Action {i}", f"v{i}") for i in range(30)], index=12)
    lines = _screen_lines([_tall_group(tmp_path, 40)], menu, height=20)
    text = "\n".join(lines)
    assert len(lines) <= 20
    assert "NAME" in text  # the table is cut from below, keeping its header
    assert re.search(r"▸ \[\w\] Action 12\b", text)
    assert "Enter" in lines[-2]  # the hint line, right above the bottom border


def test_render_without_height_draws_a_long_menu_whole(tmp_path):
    menu = dashboard.MenuState("alpha-0", [(f"Action {i}", f"v{i}") for i in range(30)], index=25)
    frame = dashboard.render(
        [_tall_group(tmp_path, 3)],
        None,
        now=datetime(2026, 6, 8, 12, 0, tzinfo=UTC),
        git_enabled=True,
        overlay=menu,
    )
    text = _render_text(frame, width=100)
    assert "Action 0 " in text and "Action 29" in text and "more" not in text


def test_render_hides_enabled_job_column_without_a_job(tmp_path):
    now = datetime(2026, 6, 8, 12, 0, tzinfo=UTC)

    # A fresh snapshot omits enabled columns with no meaningful values.
    g_noop = dashboard.RepoGroup(
        "alpha", "/repos/alpha", tmp_path / "a.yaml", [_ci("alpha-one", "alpha")]
    )
    out = _render_text(dashboard.render([g_noop], selected=None, now=now, git_enabled=True))
    # Check the header, not the empty cells.
    header_line = next(ln for ln in out.splitlines() if "NAME" in ln)
    assert " JOB " not in header_line
    # The compact phase must also be absent when no job is in flight.
    assert "clone" not in out

    # A container with an in-flight job -> JOB column present, phase value visible.
    c = _ci("alpha-two", "alpha")
    c.job_phase = "cloning"
    g_op = dashboard.RepoGroup("alpha", "/repos/alpha", tmp_path / "a.yaml", [c])
    out2 = _render_text(dashboard.render([g_op], selected=None, now=now, git_enabled=True))
    assert "clone" in out2
    header_line2 = next(ln for ln in out2.splitlines() if "NAME" in ln)
    assert " JOB " in header_line2 or header_line2.startswith("JOB ")


def test_render_shows_repo_headers_and_rows(tmp_path):
    groups = [
        dashboard.RepoGroup(
            "alpha",
            "/repos/alpha",
            tmp_path / "alpha/.jailbee/config.yaml",
            [_ci("alpha-one", "alpha")],
        ),
        dashboard.RepoGroup("gamma", None, None, [_ci("gamma-x", "gamma")]),
    ]
    now = datetime(2026, 6, 8, 12, 0, tzinfo=UTC)
    out = _render_text(
        dashboard.render(
            groups,
            selected=dashboard.Row("container", "alpha-one"),
            now=now,
            git_enabled=True,
        )
    )
    assert "alpha" in out
    assert "gamma" in out and "orphan" in out
    assert "one" in out  # display_name with prefix stripped
    assert "gamma-x" in out
    # Ordinary mode has a compact help cue, not a permanent keybinding footer.
    assert "h/? help" in out.splitlines()[0]
    assert "Enter menu" not in out and "q quit" not in out
    # The selected row is highlighted, and the highlight is its only marker.
    assert "▸" not in out
    cursor = _cursor_lines(
        _render_ansi_lines(
            dashboard.render(
                groups,
                selected=dashboard.Row("container", "alpha-one"),
                now=now,
                git_enabled=True,
            )
        )
    )
    assert len(cursor) == 1 and "one" in cursor[0] and "alpha" not in cursor[0]


def test_render_column_headers_sit_above_every_repo_heading(tmp_path):
    """The column header row belongs to the whole table, not to the first repo:
    it is drawn once, above the first repo heading, and never repeated."""
    groups = [
        dashboard.RepoGroup("alpha", "/repos/alpha", None, [_ci("alpha-one", "alpha")]),
        dashboard.RepoGroup("beta", "/repos/beta", None, [_ci("beta-two", "beta")]),
    ]
    out = _render_text(
        dashboard.render(
            groups,
            selected=None,
            now=datetime(2026, 6, 8, 12, 0, tzinfo=UTC),
            git_enabled=True,
        )
    )
    lines = out.splitlines()
    header_rows = [i for i, ln in enumerate(lines) if "NAME" in ln]
    alpha_heading = next(i for i, ln in enumerate(lines) if "▾ alpha" in ln)
    assert len(header_rows) == 1
    assert header_rows[0] < alpha_heading


def test_render_column_headers_stay_on_top_when_first_repo_is_empty(tmp_path):
    groups = [
        dashboard.RepoGroup("alpha", "/repos/alpha", None, []),
        dashboard.RepoGroup("beta", "/repos/beta", None, [_ci("beta-two", "beta")]),
    ]
    out = _render_text(
        dashboard.render(
            groups,
            selected=None,
            now=datetime(2026, 6, 8, 12, 0, tzinfo=UTC),
            git_enabled=True,
        )
    )
    lines = out.splitlines()
    header_row = next(i for i, ln in enumerate(lines) if "NAME" in ln)
    alpha_heading = next(i for i, ln in enumerate(lines) if "▾ alpha" in ln)
    assert header_row < alpha_heading


@pytest.mark.parametrize(
    ("enabled", "title", "cell"),
    [(None, "NAME", "one"), (("state", "network"), "ST", "▶")],
)
def test_render_first_column_title_aligns_with_its_cells(tmp_path, enabled, title, cell):
    """Every first-column cell carries the two-cell selection gutter, so the
    title above it must too — whichever field happens to come first."""
    group = dashboard.RepoGroup("alpha", "/repos/alpha", None, [_ci("alpha-one", "alpha")])
    out = _render_text(
        dashboard.render(
            [group],
            selected=None,
            now=datetime(2026, 6, 8, 12, 0, tzinfo=UTC),
            git_enabled=True,
            enabled=enabled,
        )
    )
    lines = out.splitlines()
    header_line = next(ln for ln in lines if title in ln)
    data_line = next(ln for ln in lines if cell in ln and "alpha" not in ln)
    assert header_line.index(title) == data_line.index(cell)


def test_render_empty_groups_shows_placeholder():
    out = _render_text(
        dashboard.render(
            [],
            selected=None,
            now=datetime(2026, 6, 8, tzinfo=UTC),
            git_enabled=False,
        )
    )
    assert "no containers" in out.lower()
    assert "no-git" in out


def test_header_uses_more_than_first_column_at_narrow_width(tmp_path):
    prefix = "long-repository-prefix"
    group = dashboard.RepoGroup(prefix, str(tmp_path), None, [_ci(f"{prefix}-one", prefix)])
    out = _render_text(
        dashboard.render(
            [group],
            selected=None,
            now=datetime(2026, 6, 8, tzinfo=UTC),
            git_enabled=True,
            enabled=("state",),
        ),
        width=48,
    )
    for rendered in (
        out,
        _render_text(
            dashboard.render(
                [group],
                selected=None,
                now=datetime(2026, 6, 8, tzinfo=UTC),
                git_enabled=True,
                enabled=("state",),
            ),
            width=100,
        ),
    ):
        heading_line = next(line for line in rendered.splitlines() if prefix in line)
        data_line = next(line for line in rendered.splitlines() if "▶" in line)
        assert len(prefix) > len("▶")
        assert heading_line.index("▾") < data_line.index("▶")


def test_render_empty_repo_shows_header_without_table(tmp_path):
    group = dashboard.RepoGroup("empty", str(tmp_path), None, [])
    out = _render_text(
        dashboard.render(
            [group],
            selected=None,
            now=datetime(2026, 6, 8, tzinfo=UTC),
            git_enabled=True,
        )
    )
    assert "empty" in out
    assert "(0)" in out
    assert "no containers found" not in out
    assert "NAME" not in out


def test_render_all_filtered_repos_explains_visibility_settings(tmp_path):
    out = _render_text(
        dashboard.render(
            [],
            selected=None,
            now=datetime(2026, 6, 8, tzinfo=UTC),
            git_enabled=True,
            hidden_by_preferences=True,
        )
    )
    assert "visibility" in out.lower()
    assert "settings" in out.lower()


def test_narrow_multi_column_render_stays_within_available_content_width(tmp_path):
    group = dashboard.RepoGroup(
        "long-repository-prefix",
        str(tmp_path),
        None,
        [_ci("long-repository-prefix-one", "long-repository-prefix")],
    )
    rendered = _render_text(
        dashboard.render(
            [group],
            selected=None,
            now=datetime(2026, 6, 8, tzinfo=UTC),
            git_enabled=True,
            enabled=("state", "network", "name"),
        ),
        width=36,
    )
    table_lines = [line for line in rendered.splitlines() if "▶" in line]
    assert table_lines
    assert max(len(line) for line in table_lines) <= 36


def _wide_group(tmp_path):
    return dashboard.RepoGroup(
        "alpha",
        str(tmp_path),
        None,
        [
            dataclasses.replace(
                _ci("alpha-one", "alpha", pr_number=4, mode="mount"),
                created_at=datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
            )
        ],
    )


_WIDE = ("name", "state", "network", "mode", "pr", "created")


def _frame_at(groups, *, width, offset=0, selected=None, folded=frozenset(), enabled=_WIDE):
    return _render_text(
        dashboard.render(
            groups,
            selected,
            now=datetime(2026, 6, 8, tzinfo=UTC),
            git_enabled=True,
            enabled=enabled,
            folded=folded,
            column_offset=offset,
        ),
        width=width,
    )


def _header(text):
    return next(line for line in text.splitlines() if "NAME" in line)


def test_render_never_drops_a_column_and_scrolling_reaches_them_all(tmp_path):
    group = _wide_group(tmp_path)
    wide = _header(_frame_at([group], width=200)).split()
    assert set(wide) == {"│", "NAME", "MODE", "ST", "AGE", "NET", "PR"}
    narrow0 = _header(_frame_at([group], width=40))
    assert not all(title in narrow0.split() for title in wide)  # really overflows
    seen = set()
    for offset in range(len(wide) + 1):
        seen |= set(_header(_frame_at([group], width=40, offset=offset)).split())
    assert set(wide) <= seen


def test_render_keeps_column_widths_stable_across_terminal_widths(tmp_path):
    group = _wide_group(tmp_path)
    narrow, wide = _header(_frame_at([group], width=60)), _header(_frame_at([group], width=200))
    assert narrow.index("ST") == wide.index("ST")
    assert narrow.index("NET") == wide.index("NET")


def test_render_lines_never_exceed_the_width_and_marks_show(tmp_path):
    group = _wide_group(tmp_path)
    at0 = _frame_at([group], width=40)
    at1 = _frame_at([group], width=40, offset=1)
    assert all(len(line) <= 40 for line in (at0 + at1).splitlines())
    assert "\u203a" in _header(at0) and "\u2039" not in _header(at0)
    assert "\u2039" in _header(at1)


def test_render_scrolled_header_and_rows_stay_aligned(tmp_path):
    group = _wide_group(tmp_path)
    aligned = 0
    for offset in (0, 1, 2):
        out = _frame_at([group], width=40, offset=offset).splitlines()
        header = _header("\n".join(out))
        row = next(line for line in out if "one" in line)
        if offset:
            assert "\u2039" in header
        for title, value in (("MODE", "mnt"), ("ST", "▶"), ("AGE", "6d"), ("NET", "●")):
            if title in header:
                assert header.index(title) == row.index(value)
                aligned += 1
    assert aligned >= 3


def test_render_highlight_stays_on_row_when_scrolled(tmp_path, monkeypatch):
    monkeypatch.setenv("TERM", "xterm-256color")
    group = _wide_group(tmp_path)
    frame = dashboard.render(
        [group],
        dashboard.Row("container", "alpha-one"),
        now=datetime(2026, 6, 8, tzinfo=UTC),
        git_enabled=True,
        enabled=_WIDE,
        column_offset=2,
    )
    cursor = _cursor_lines(_render_ansi_lines(frame, width=40))
    assert len(cursor) == 1 and "one" in dashboard.Text.from_ansi(cursor[0]).plain
    assert "\u2039" in "\n".join(_render_ansi_lines(frame, width=40))


def test_render_narrower_than_the_name_column(tmp_path):
    group = _wide_group(tmp_path)
    out = _frame_at([group], width=14, offset=3)
    assert all(len(line) <= 14 for line in out.splitlines())
    assert "\u2039" not in out and "\u203a" not in out


def test_render_scrolled_with_every_repo_folded(tmp_path):
    group = _wide_group(tmp_path)
    out = _frame_at([group], width=40, offset=3, folded=frozenset({"alpha"}))
    assert "alpha" in out and "\u2039" not in out and "\u203a" not in out


def test_render_keeps_only_enabled_column_at_tiny_width(tmp_path):
    group = dashboard.RepoGroup("alpha", str(tmp_path), None, [_ci("alpha-one", "alpha")])
    frame = dashboard.render(
        [group],
        selected=None,
        now=datetime(2026, 6, 8, tzinfo=UTC),
        git_enabled=True,
        enabled=("state",),
    )

    assert "ST" in _render_text(frame, width=20)


def test_render_column_offsets_align_across_repos_of_different_lengths(tmp_path):
    groups = [
        dashboard.RepoGroup("a", "/a", None, [_ci("a-one", "a")]),
        dashboard.RepoGroup(
            "a-much-longer-repository",
            "/b",
            None,
            [_ci("a-much-longer-repository-two", "a-much-longer-repository")],
        ),
    ]
    out = _render_text(
        dashboard.render(
            groups,
            selected=None,
            now=datetime(2026, 6, 8, tzinfo=UTC),
            git_enabled=True,
            enabled=("state",),
        )
    )
    data_lines = [line for line in out.splitlines() if "▶" in line]
    assert len(data_lines) == 2
    assert [line.index("▶") for line in data_lines] == [data_lines[0].index("▶")] * 2


def test_render_forwards_enabled_columns_to_visible_fields(tmp_path):
    now = datetime(2026, 6, 8, 12, 0, tzinfo=UTC)
    g = dashboard.RepoGroup(
        "alpha",
        "/repos/alpha",
        tmp_path / "a.yaml",
        [
            dataclasses.replace(
                _ci("alpha-one", "alpha"), created_at=datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
            )
        ],
    )
    out = _render_text(
        dashboard.render(
            [g],
            selected=None,
            now=now,
            git_enabled=True,
            enabled=["name", "created"],
        )
    )
    header_line = next(ln for ln in out.splitlines() if "NAME" in ln)
    assert "AGE" in header_line
    assert "STATE" not in header_line


def _title_line(groups, *, git_enabled: bool = True, **kwargs) -> str:
    """The panel's top border line, which carries the dashboard title."""
    out = _render_text(
        dashboard.render(
            groups,
            selected=kwargs.pop("selected", None),
            now=datetime(2026, 6, 8, 12, 0, tzinfo=UTC),
            git_enabled=git_enabled,
            **kwargs,
        )
    )
    return next(ln for ln in out.splitlines() if "jailbee dashboard" in ln)


def test_render_title_has_no_blinking_refresh_indicator(tmp_path):
    """The old `⟳` marker toggled on every gather, re-centring the whole title."""
    g = dashboard.RepoGroup(
        "alpha", "/repos/alpha", tmp_path / "a.yaml", [_ci("alpha-one", "alpha")]
    )
    assert "⟳" not in _title_line([g])


def test_render_title_is_left_aligned(tmp_path):
    """Left-aligned, so a widening title grows rightwards instead of shifting."""
    g = dashboard.RepoGroup(
        "alpha", "/repos/alpha", tmp_path / "a.yaml", [_ci("alpha-one", "alpha")]
    )
    line = _title_line([g])
    assert line.index("🐝 jailbee dashboard") <= 3


def test_render_title_carries_the_no_git_marker(tmp_path):
    """`--no-git` is constant for the run, so it belongs in the title."""
    g = dashboard.RepoGroup(
        "alpha", "/repos/alpha", tmp_path / "a.yaml", [_ci("alpha-one", "alpha")]
    )
    assert "no-git" in _title_line([g], git_enabled=False)
    assert "no-git" not in _title_line([g], git_enabled=True)


def test_render_subtitle_is_empty_without_a_notice(tmp_path):
    """The refresh timing moved into the title; the subtitle is notice-only."""
    g = dashboard.RepoGroup(
        "alpha", "/repos/alpha", tmp_path / "a.yaml", [_ci("alpha-one", "alpha")]
    )
    out = _render_text(
        dashboard.render(
            [g],
            selected=None,
            now=datetime(2026, 6, 8, 12, 0, tzinfo=UTC),
            git_enabled=True,
        )
    )
    assert "refreshed" not in out


def test_render_long_notice_wraps_below_the_table_instead_of_the_border(tmp_path):
    """A long CLI message is shown whole, not cut on the bottom border."""
    g = dashboard.RepoGroup(
        "alpha", "/repos/alpha", tmp_path / "a.yaml", [_ci("alpha-one", "alpha")]
    )
    notice = "✗ invalid credential group name 'Bad Name': " + "lowercase letters " * 12 + "END"
    lines = (
        _render_text(
            dashboard.render(
                [g],
                selected=None,
                now=datetime(2026, 6, 8, 12, 0, tzinfo=UTC),
                git_enabled=True,
                notice=notice,
            ),
            width=100,
        )
        .rstrip()
        .splitlines()
    )
    table_row = next(i for i, ln in enumerate(lines) if "▶" in ln)
    first = next(i for i, ln in enumerate(lines) if "✗ invalid credential group name" in ln)
    assert first > table_row
    assert "END" in "".join(lines[first:-1])
    assert "✗" not in lines[-1] and "…" not in lines[-1]


def test_render_notice_with_square_brackets_is_not_markup(tmp_path):
    g = dashboard.RepoGroup(
        "alpha", "/repos/alpha", tmp_path / "a.yaml", [_ci("alpha-one", "alpha")]
    )
    out = _render_text(
        dashboard.render(
            [g],
            selected=None,
            now=datetime(2026, 6, 8, 12, 0, tzinfo=UTC),
            git_enabled=True,
            notice="bad [/x] value",
        )
    )
    assert "bad [/x] value" in out


def test_render_keeps_the_table_visible_under_the_menu_overlay(tmp_path):
    """The point of the inline menu: the dashboard stays on screen behind it."""
    g = dashboard.RepoGroup(
        "alpha",
        "/repos/alpha",
        tmp_path / "a.yaml",
        [_ci("alpha-one", "alpha"), _ci("alpha-two", "alpha")],
    )
    menu = dashboard.MenuState(
        "alpha-one", [("Attach tmux", "tmux"), ("Outbox", "outbox browse")], index=1
    )
    out = _render_text(
        dashboard.render(
            [g],
            selected=dashboard.Row("container", "alpha-one"),
            now=datetime(2026, 6, 8, 12, 0, tzinfo=UTC),
            git_enabled=True,
            overlay=menu,
        )
    )
    # Both container rows and the column headers are still rendered.
    assert "one" in out and "two" in out
    assert "NAME" in out
    # The menu lists its actions, titled with the target container.
    assert "Attach tmux" in out and "Outbox" in out
    assert "alpha-one" in out
    # The highlighted entry (index=1) carries the cursor, the other does not.
    cursor_line = next(ln for ln in out.splitlines() if "Outbox" in ln)
    other_line = next(ln for ln in out.splitlines() if "Attach tmux" in ln)
    assert "▸" in cursor_line
    assert "▸" not in other_line


def test_normal_mode_help_is_in_frame_not_footer(tmp_path):
    g = dashboard.RepoGroup(
        "alpha", "/repos/alpha", tmp_path / "a.yaml", [_ci("alpha-one", "alpha")]
    )
    kwargs = {
        "selected": dashboard.Row("container", "alpha-one"),
        "now": datetime(2026, 6, 8, 12, 0, tzinfo=UTC),
        "git_enabled": True,
    }
    browsing = _render_text(dashboard.render([g], **kwargs))
    menu_open = _render_text(
        dashboard.render(
            [g],
            **kwargs,
            overlay=dashboard.MenuState("alpha-one", [("Attach tmux", "tmux")], index=0),
        )
    )
    assert "h/? help" in browsing.splitlines()[0]
    assert "Enter menu" not in browsing
    assert "Space fold" not in browsing
    assert "Esc" in menu_open and "cancel" in menu_open


def test_small_width_keeps_help_cue_in_the_top_border(tmp_path):
    g = dashboard.RepoGroup("alpha", str(tmp_path), None, [_ci("alpha-one", "alpha")])
    out = _render_text(
        dashboard.render(
            [g],
            selected=None,
            now=datetime(2026, 6, 8, tzinfo=UTC),
            git_enabled=True,
        ),
        width=42,
    )
    assert "h/? help" in out.splitlines()[0]


def test_render_shows_a_notice_and_omits_it_when_none(tmp_path):
    g = dashboard.RepoGroup(
        "alpha", "/repos/alpha", tmp_path / "a.yaml", [_ci("alpha-one", "alpha")]
    )
    kwargs = {
        "selected": None,
        "now": datetime(2026, 6, 8, 12, 0, tzinfo=UTC),
        "git_enabled": True,
    }
    with_notice = _render_text(dashboard.render([g], **kwargs, notice="alpha-one is view-only"))
    without = _render_text(dashboard.render([g], **kwargs))

    assert "view-only" in with_notice
    assert "view-only" not in without


# --- terminal (xterm/tmux) window title -----------------------------------------


def _title_groups(tmp_path):
    return [
        dashboard.RepoGroup(
            "alpha", "/repos/alpha", tmp_path / "a.yaml", [_ci("alpha-one", "alpha")]
        ),
        dashboard.RepoGroup("gamma", None, None, [_ci("gamma-x", "gamma")]),
    ]


def test_terminal_title_names_the_repo_and_the_selected_container(tmp_path):
    groups = _title_groups(tmp_path)
    title = dashboard.terminal_title(groups, dashboard.Row("container", "alpha-one"))
    assert title == "🐝 alpha/one"


def test_terminal_title_on_a_repo_header_names_only_the_repo(tmp_path):
    groups = _title_groups(tmp_path)
    assert dashboard.terminal_title(groups, dashboard.Row("repo", "alpha")) == "🐝 alpha"


def test_terminal_title_uses_the_full_name_for_an_orphan_container(tmp_path):
    """An orphan group stripped no prefix, so neither does the title —
    matching the NAME column."""
    groups = _title_groups(tmp_path)
    title = dashboard.terminal_title(groups, dashboard.Row("container", "gamma-x"))
    assert title == "🐝 gamma/gamma-x"


def test_terminal_title_without_a_selection_falls_back_to_the_tool_name(tmp_path):
    assert dashboard.terminal_title(_title_groups(tmp_path), None) == "🐝 jailbee"


def test_terminal_title_of_an_unknown_container_falls_back(tmp_path):
    groups = _title_groups(tmp_path)
    assert dashboard.terminal_title(groups, dashboard.Row("container", "ghost")) == "🐝 jailbee"


def test_set_terminal_title_writes_one_osc2_sequence():
    stream = io.StringIO()
    dashboard.set_terminal_title("🐝 alpha/one", stream=stream)
    assert stream.getvalue() == "\x1b]2;🐝 alpha/one\x07"


def test_terminal_title_scope_pushes_on_entry_and_pops_on_exit():
    """Without the pop the terminal keeps the bee title after `q`."""
    stream = io.StringIO()
    with dashboard.terminal_title_scope(stream):
        assert stream.getvalue() == "\x1b[22;2t"
    assert stream.getvalue() == "\x1b[22;2t\x1b[23;2t"


def test_terminal_title_scope_pops_even_when_the_body_raises():
    stream = io.StringIO()
    with contextlib.suppress(RuntimeError), dashboard.terminal_title_scope(stream):
        raise RuntimeError("boom")
    assert stream.getvalue().endswith("\x1b[23;2t")


# --- creating a container from the dashboard --------------------------------


def _create_groups(tmp_path):
    return [
        dashboard.RepoGroup(
            "alpha", "/repos/alpha", tmp_path / "a.yaml", [_ci("alpha-one", "alpha")]
        ),
        dashboard.RepoGroup("gamma", None, None, [_ci("gamma-x", "gamma")]),
    ]


def test_new_container_target_from_a_container_row(tmp_path):
    groups = _create_groups(tmp_path)
    target = dashboard.new_container_target(groups, dashboard.Row("container", "alpha-one"))
    assert target is groups[0]


def test_new_container_target_from_a_repo_header(tmp_path):
    groups = _create_groups(tmp_path)
    assert dashboard.new_container_target(groups, dashboard.Row("repo", "alpha")) is groups[0]


def test_new_container_target_is_none_without_a_selection(tmp_path):
    assert dashboard.new_container_target(_create_groups(tmp_path), None) is None


def test_new_container_target_is_none_for_a_stale_selection(tmp_path):
    groups = _create_groups(tmp_path)
    assert dashboard.new_container_target(groups, dashboard.Row("container", "ghost")) is None
    assert dashboard.new_container_target(groups, dashboard.Row("repo", "ghost")) is None


def test_new_container_target_is_none_for_an_orphan_group(tmp_path):
    """An orphan group has no repo config to create against — the same reason
    it gets no action menu."""
    groups = _create_groups(tmp_path)
    assert dashboard.new_container_target(groups, dashboard.Row("container", "gamma-x")) is None
    assert dashboard.new_container_target(groups, dashboard.Row("repo", "gamma")) is None


def test_new_container_target_accepts_a_scratch_group():
    """A repo with no config file has a root to create in — `jailbee new` run
    there synthesizes the same config the dashboard displayed."""
    groups = [dashboard.RepoGroup("delta", "/repos/delta", None, [_ci("delta-x", "delta")])]

    assert dashboard.new_container_target(groups, dashboard.Row("repo", "delta")) is groups[0]
    assert (
        dashboard.new_container_target(groups, dashboard.Row("container", "delta-x")) is groups[0]
    )


def test_new_container_reject_note_for_prefix_is_none_for_a_scratch_group():
    groups = [dashboard.RepoGroup("delta", "/repos/delta", None, [_ci("delta-x", "delta")])]

    assert dashboard.new_container_reject_note_for_prefix(groups, "delta") is None


def test_new_container_reject_note_is_none_when_creation_is_possible(tmp_path):
    groups = _create_groups(tmp_path)
    assert dashboard.new_container_reject_note(groups, dashboard.Row("repo", "alpha")) is None


def test_new_container_reject_note_asks_for_a_selection(tmp_path):
    note = dashboard.new_container_reject_note(_create_groups(tmp_path), None)
    assert note is not None and "elect" in note


def test_new_container_reject_note_names_the_orphan_repo(tmp_path):
    """'Nothing happened' is indistinguishable from broken — the note has to
    say which repo has no config."""
    groups = _create_groups(tmp_path)
    note = dashboard.new_container_reject_note(groups, dashboard.Row("repo", "gamma"))
    assert note is not None and "gamma" in note


def test_new_container_reject_note_says_a_vanished_repo_row_is_gone(tmp_path):
    """A repo row whose group disappeared between frames must not be told to
    "select a repo" — one was selected. Same wording the container branch
    uses for the same cause."""
    groups = _create_groups(tmp_path)
    note = dashboard.new_container_reject_note(groups, dashboard.Row("repo", "ghost"))
    assert note == "'ghost' is no longer listed"


def test_new_container_reject_note_phrases_a_vanished_row_the_same_way(tmp_path):
    """The two row kinds must not describe one situation differently."""
    groups = _create_groups(tmp_path)
    assert dashboard.new_container_reject_note(
        groups, dashboard.Row("repo", "ghost")
    ) == dashboard.new_container_reject_note(groups, dashboard.Row("container", "ghost"))


def test_new_container_reject_note_for_prefix_is_none_when_creation_is_possible(tmp_path):
    groups = _create_groups(tmp_path)
    assert dashboard.new_container_reject_note_for_prefix(groups, "alpha") is None


def test_new_container_reject_note_for_prefix_asks_for_a_selection_when_empty(tmp_path):
    note = dashboard.new_container_reject_note_for_prefix(_create_groups(tmp_path), "")
    assert note is not None and "elect" in note


def test_new_container_reject_note_for_prefix_asks_for_a_selection_when_unknown(tmp_path):
    note = dashboard.new_container_reject_note_for_prefix(_create_groups(tmp_path), "ghost")
    assert note is not None and "elect" in note


def test_new_container_reject_note_for_prefix_names_the_orphan_repo(tmp_path):
    groups = _create_groups(tmp_path)
    note = dashboard.new_container_reject_note_for_prefix(groups, "gamma")
    assert note is not None and "gamma" in note


@pytest.fixture(autouse=True)
def _no_real_branch_listing(mocker):
    """Keep the base prompt's branch listing from reaching a patched ``subprocess.run``."""
    mocker.patch("jailbee.git.list_branches", return_value=[])


def test_host_branches_lists_the_groups_repo_minus_the_excluded(mocker, tmp_path):
    lb = mocker.patch("jailbee.git.list_branches", return_value=["main", "feat/a", "dev"])
    assert dashboard.host_branches(str(tmp_path), exclude="feat/a") == ("main", "dev")
    lb.assert_called_once_with(tmp_path)


def test_host_branches_is_empty_without_a_repo_root(mocker):
    lb = mocker.patch("jailbee.git.list_branches")
    assert dashboard.host_branches(None) == ()
    lb.assert_not_called()


def test_retarget_argv_puts_the_names_after_the_separator():
    assert dashboard.dact.retarget_argv("alpha-x", "main") == [
        "git",
        "retarget",
        "--",
        "alpha-x",
        "main",
    ]


def test_new_container_base_default_reads_the_groups_own_repo(mocker, tmp_path):
    """Cross-repo dashboards: the branch offered must come from the row's
    repo, not the process's cwd."""
    get = mocker.patch("jailbee.git.get_current_branch", return_value="config-improvements")

    assert dashboard.new_container_base_default(str(tmp_path)) == "config-improvements"
    assert get.call_args.args[0] == Path(str(tmp_path))


def test_new_container_base_default_is_none_on_a_detached_head(mocker, tmp_path):
    mocker.patch("jailbee.git.get_current_branch", return_value=None)
    assert dashboard.new_container_base_default(str(tmp_path)) is None


def test_new_container_base_default_is_none_without_a_repo_root(mocker):
    """An orphan group has no root to read; git must not be invoked at all."""
    get = mocker.patch("jailbee.git.get_current_branch")
    assert dashboard.new_container_base_default(None) is None
    get.assert_not_called()


def test_new_container_argv_passes_the_base_positionally(tmp_path):
    """`--from-base` is the golden-image alias, not a git base. The base
    branch is `jailbee new`'s second positional or it is nothing."""
    config_path = tmp_path / ".jailbee" / "config.yaml"
    target = dashboard.RepoTarget(tmp_path, config_path)
    assert dashboard.new_container_argv(target, "dashboard-fixes", "config-improvements") == [
        "jailbee",
        "new",
        "--config",
        str(config_path),
        "--background",
        "--",
        "dashboard-fixes",
        "config-improvements",
    ]


def test_new_container_argv_omits_config_for_a_scratch_repo(tmp_path):
    """No file to point `--config` at — the caller runs it in the repo root."""
    argv = dashboard.new_container_argv(dashboard.RepoTarget(tmp_path, None), "feat/x", "main")

    assert argv == ["jailbee", "new", "--background", "--", "feat/x", "main"]


@pytest.mark.parametrize("answer", ["--mount", "--yes", "--config=/tmp/evil.yaml", "-m"])
def test_new_container_argv_never_reads_a_typed_answer_as_an_option(tmp_path, answer):
    """Branch and base are typed free text. Without `--`, a branch "--mount"
    would give the container the host repo read-write."""
    for branch, base in ((answer, "main"), ("feat", answer)):
        argv = dashboard.new_container_argv(dashboard.RepoTarget(tmp_path, None), branch, base)
        separator = argv.index("--")
        assert argv[separator + 1 :] == [branch, base]


def test_new_container_argv_separator_really_stops_option_parsing(tmp_path):
    """Parse with the real `jailbee new` command, so the `--` is proven to be
    honoured by Click rather than merely present in the list."""
    from typer.main import get_command

    from jailbee.cli import app as cli_app

    argv = dashboard.new_container_argv(dashboard.RepoTarget(tmp_path, None), "--mount", "--yes")
    command = get_command(cli_app).commands["new"]  # type: ignore[attr-defined]

    ctx = command.make_context("new", argv[2:])

    assert ctx.params["background"] is True
    assert ctx.params["mount"] is False
    assert ctx.params["yes"] is False
    assert ctx.params["container_branch"] == "--mount"


def test_new_container_argv_carries_no_yes_flag(tmp_path):
    """--yes would accept a network-widening branch autostart config unseen."""
    argv = dashboard.new_container_argv(_dispatch_target(tmp_path, "c.yaml"), "b", "base")
    assert "--yes" not in argv and "-y" not in argv


def test_new_pr_container_argv_targets_configured_repo_without_yes(tmp_path):
    target = _dispatch_target(tmp_path, "c.yaml")

    assert dashboard.new_pr_container_argv(target, 123) == [
        "jailbee",
        "new",
        "--config",
        str(target.config_path),
        "--background",
        "--pr",
        "123",
    ]


def test_new_pr_container_argv_targets_scratch_repo(tmp_path):
    assert dashboard.new_pr_container_argv(dashboard.RepoTarget(tmp_path, None), 123) == [
        "jailbee",
        "new",
        "--background",
        "--pr",
        "123",
    ]


def test_parse_key_maps_arrows_and_letters():
    assert dashboard.parse_key(b"\x1b[A") == "up"
    assert dashboard.parse_key(b"\x1b[B") == "down"
    assert dashboard.parse_key(b"k") == "up"
    assert dashboard.parse_key(b"j") == "down"
    assert dashboard.parse_key(b"\r") == "enter"
    assert dashboard.parse_key(b"\n") == "enter"
    assert dashboard.parse_key(b"r") == "refresh"
    assert dashboard.parse_key(b"q") == "quit"
    assert dashboard.parse_key(b"Z") == ""  # unmapped


def test_parse_key_maps_the_quick_action_keys():
    assert dashboard.parse_key(b"t") == "action:tmux"
    assert dashboard.parse_key(b"s") == "action:shell"
    assert dashboard.parse_key(b"i") == "action:ide"
    assert dashboard.parse_key(b"c") == "action:chrome"
    assert dashboard.parse_key(b"p") == "action:pr"
    assert dashboard.parse_key(b"h") == "help"
    assert dashboard.parse_key(b"?") == "help"


def test_parse_key_maps_the_workflow_action_keys():
    assert dashboard.parse_key(b"P") == "action:pr-update"
    assert dashboard.parse_key(b"u") == "action:push"
    assert dashboard.parse_key(b"d") == "action:diff"
    assert dashboard.parse_key(b"D") == "action:destroy"


def test_quick_verb_destroy_key_follows_the_menu_gate(tmp_path):
    running = dashboard.RepoGroup(
        "alpha", str(tmp_path), tmp_path / "c.yaml", [_ci("alpha-x", "alpha")]
    )
    orphan = dashboard.RepoGroup("gone", None, None, [_ci("gone-x", "gone")])

    assert dashboard.quick_verb([running], "alpha-x", "action:destroy") == "destroy"
    assert dashboard.quick_verb([orphan], "gone-x", "action:destroy") is None


def test_quick_verb_separates_open_pr_from_update_pr(tmp_path):
    """`p` opens the PR in a browser, `P` pushes to it — the gate matches the
    verb exactly, so the two never collapse into one another."""
    with_pr = dashboard.RepoGroup(
        "alpha",
        str(tmp_path),
        tmp_path / "c.yaml",
        [_ci("alpha-x", "alpha", pr_number=7)],
    )

    assert dashboard.quick_verb([with_pr], "alpha-x", "action:pr") == "pr --open"
    assert dashboard.quick_verb([with_pr], "alpha-x", "action:pr-update") == "pr"


def test_quick_verb_workflow_keys_follow_the_menu_gate(tmp_path):
    running = dashboard.RepoGroup(
        "alpha", str(tmp_path), tmp_path / "c.yaml", [_ci("alpha-x", "alpha")]
    )
    mounted = dashboard.RepoGroup(
        "alpha", str(tmp_path), tmp_path / "c.yaml", [_ci("alpha-m", "alpha", mode="mount")]
    )
    clean = dashboard.RepoGroup(
        "alpha",
        str(tmp_path),
        tmp_path / "c.yaml",
        [_ci("alpha-c", "alpha", git_status=_dirty(wt="clean", ahead_count="0"))],
    )

    assert dashboard.quick_verb([running], "alpha-x", "action:push") == "git push"
    assert dashboard.quick_verb([running], "alpha-x", "action:diff") == "git diff"
    assert dashboard.quick_verb([mounted], "alpha-m", "action:push") is None
    assert dashboard.quick_verb([clean], "alpha-c", "action:diff") is None


def test_key_bindings_are_the_only_source_of_parse_key():
    """Every declared key sequence parses to its binding's token, and nothing
    is declared twice — the table is what `parse_key` is built from."""
    seen: dict[bytes, str] = {}
    for b in dashboard.KEY_BINDINGS:
        assert b.keys, f"{b.token} declares no keys"
        for key in b.keys:
            assert key not in seen, f"{key!r} bound twice ({seen.get(key)} and {b.token})"
            seen[key] = b.token
            assert dashboard.parse_key(key) == b.token
    tokens = [b.token for b in dashboard.KEY_BINDINGS]
    assert len(tokens) == len(set(tokens))


def test_every_quick_action_verb_is_a_real_menu_verb():
    """Guards against a typo'd verb in the key table.

    A quick key that dispatches a verb `menu_actions` never offers could never
    fire (the gate below filters it out), so the bug would be silent.
    """
    offered = set()
    for state in ("Running", "Stopped", "Frozen"):
        offered |= {
            verb
            for _label, verb in dashboard.menu_actions(
                _ctx(
                    state=state,
                    apps=_apps("ide", "chrome"),
                    pr_number=7,
                    job_clearable=True,
                    has_job=True,
                )
            )
        }
    quick = {b.verb for b in dashboard.KEY_BINDINGS if b.verb is not None}
    assert quick, "no quick-action keys declared"
    assert quick <= offered, f"unknown verbs: {quick - offered}"


def test_quick_verb_returns_the_verb_when_the_action_is_offered(tmp_path):
    group = dashboard.RepoGroup(
        "alpha", str(tmp_path), tmp_path / "c.yaml", [_ci("alpha-x", "alpha")]
    )

    assert dashboard.quick_verb([group], "alpha-x", "action:tmux") == "tmux"
    assert dashboard.quick_verb([group], "alpha-x", "action:shell") == "shell"


def test_quick_verb_is_none_when_the_action_is_not_offered(tmp_path):
    """The gate is `actions_for_container`, so every rule lives in
    `menu_actions` alone — no second copy of "when is tmux allowed"."""
    running = dashboard.RepoGroup(
        "alpha", str(tmp_path), tmp_path / "c.yaml", [_ci("alpha-x", "alpha")]
    )
    stopped = dashboard.RepoGroup(
        "alpha", str(tmp_path), tmp_path / "c.yaml", [_ci("alpha-x", "alpha", state="Stopped")]
    )
    orphan = dashboard.RepoGroup("gamma", None, None, [_ci("gamma-x", "gamma")])

    assert dashboard.quick_verb([stopped], "alpha-x", "action:tmux") is None  # not running
    assert dashboard.quick_verb([running], "alpha-x", "action:ide") is None  # jetbrains off
    assert dashboard.quick_verb([running], "alpha-x", "action:chrome") is None  # chrome off
    assert dashboard.quick_verb([running], "alpha-x", "action:pr") is None  # no PR known
    assert dashboard.quick_verb([orphan], "gamma-x", "action:tmux") is None  # view-only
    assert dashboard.quick_verb([running], "alpha-nope", "action:tmux") is None  # unknown
    assert dashboard.quick_verb([running], None, "action:tmux") is None
    assert dashboard.quick_verb([running], "alpha-x", "refresh") is None  # not an action key


def test_quick_verb_follows_the_repos_apps(tmp_path):
    group = dashboard.RepoGroup(
        "alpha",
        str(tmp_path),
        tmp_path / "c.yaml",
        [_ci("alpha-x", "alpha")],
        apps=_apps("ide", "chrome"),
    )

    assert dashboard.quick_verb([group], "alpha-x", "action:ide") == "ide"
    assert dashboard.quick_verb([group], "alpha-x", "action:chrome") == "chrome"


def test_quick_reject_note_names_the_key_and_the_container(tmp_path):
    stopped = dashboard.RepoGroup(
        "alpha", str(tmp_path), tmp_path / "c.yaml", [_ci("alpha-x", "alpha", state="Stopped")]
    )

    note = dashboard.quick_reject_note([stopped], "alpha-x", "action:tmux")

    assert "'t'" in note and "tmux" in note and "alpha-x" in note


def test_quick_reject_note_reports_remote_policy_denial(tmp_path):
    from jailbee.config.models_remote import RemoteCommandPolicy, RemoteSSHConfig

    group = dashboard.RepoGroup("alpha", str(tmp_path), None, [_ci("alpha-x", "alpha")])
    policy = RemoteSSHConfig(commands=RemoteCommandPolicy(mode="allowlist", allow=["git merge"]))

    note = dashboard.quick_reject_note(
        [group], "alpha-x", "action:push", ssh_policy=policy, over_ssh=True
    )

    assert note == "Jailbee command is not allowed: git push"


def test_quick_reject_note_prefers_the_view_only_explanation():
    orphan = dashboard.RepoGroup("gamma", None, None, [_ci("gamma-x", "gamma")])

    assert dashboard.quick_reject_note(
        [orphan], "gamma-x", "action:tmux"
    ) == dashboard.view_only_note([orphan], "gamma-x")


def test_quick_reject_note_handles_an_empty_selection():
    assert "selected" in dashboard.quick_reject_note([], None, "action:tmux")


def test_binding_for_token_finds_the_key_and_its_label():
    binding = dashboard.binding_for_token("action:tmux")

    assert binding is not None
    assert binding.hint == "t"
    assert binding.label
    assert dashboard.binding_for_token("nope") is None


def test_render_help_overlay_documents_every_key(tmp_path):
    g = dashboard.RepoGroup(
        "alpha", "/repos/alpha", tmp_path / "a.yaml", [_ci("alpha-one", "alpha")]
    )
    out = _render_text(
        dashboard.render(
            [g],
            selected=dashboard.Row("container", "alpha-one"),
            now=datetime(2026, 6, 8, 12, 0, tzinfo=UTC),
            git_enabled=True,
            overlay="help",
        )
    )
    for b in dashboard.KEY_BINDINGS:
        if b.hint:
            assert b.hint in out, f"{b.token}: hint {b.hint!r} missing from help"
            assert b.label in out, f"{b.token}: label {b.label!r} missing from help"
    assert "open a container or repo menu (fold there)" in out
    assert "fold/unfold the selected repo (Settings: toggle)" in out
    # Help replaces neither the table nor the hint line, and explains gating.
    assert "NAME" in out and "one" in out
    assert "offered" in out or "available" in out
    assert "close" in out
    assert "Egress panel: a adds, r removes a scoped override" in out
    assert "Menus: the key in brackets picks that entry" in out


def test_render_swaps_the_hint_line_while_the_menu_is_open(tmp_path):
    g = dashboard.RepoGroup(
        "alpha", "/repos/alpha", tmp_path / "a.yaml", [_ci("alpha-one", "alpha")]
    )
    out = _render_text(
        dashboard.render(
            [g],
            selected=dashboard.Row("container", "alpha-one"),
            now=datetime(2026, 6, 8, 12, 0, tzinfo=UTC),
            git_enabled=True,
            overlay=dashboard.MenuState("alpha-one", [("Attach tmux", "tmux")]),
        )
    )
    assert "Enter open/run" in out and "Esc cancel" in out
    assert "[key] pick" in out
    assert "h/? help" in out.splitlines()[0]


def test_render_menu_submenu_title_and_contextual_back_hint(tmp_path):
    g = dashboard.RepoGroup("alpha", "/repos/alpha", tmp_path / "a.yaml", [_ci("alpha-x", "alpha")])
    root = _grouped_menu()
    submenu, _ = dashboard.enter_menu(dashboard.move_menu(dashboard.move_menu(root, 1), 1))

    def frame(menu):
        return _render_text(
            dashboard.render(
                [g],
                selected=dashboard.Row("container", "alpha-x"),
                now=datetime(2026, 6, 8, 12, 0, tzinfo=UTC),
                git_enabled=True,
                overlay=menu,
            )
        )

    assert "PR →" in frame(root) and "Git →" in frame(root)
    assert "Enter open/run" in frame(root)
    assert "Esc cancel" in frame(root)
    assert "alpha-x → PR" in frame(submenu)
    assert "Create/update PR" in frame(submenu)
    assert "Enter run" in frame(submenu)
    assert "Esc back" in frame(submenu)
    assert "Git →" not in frame(submenu)


def test_parse_key_separates_escape_from_interrupt():
    """Esc/q close an overlay; Ctrl-C and EOF must always end the dashboard.

    A single token for all of them would make Ctrl-C merely close the action
    menu, leaving no way out while an overlay is open.
    """
    assert dashboard.parse_key(b"\x1b") == "cancel"  # bare Esc (arrows are \x1b[…)
    assert dashboard.parse_key(b"\x03") == "interrupt"  # Ctrl-C
    assert dashboard.parse_key(b"") == "interrupt"  # EOF (stdin closed)


# ---------------------------------------------------------------------------
# CLI wiring test
# ---------------------------------------------------------------------------

from typer.testing import CliRunner  # noqa: E402

from jailbee.cli import app  # noqa: E402


def test_render_shows_memory_used_and_limit(tmp_path):
    c = _ci("alpha-one", "alpha")
    c.memory_usage = 4_000_000_000
    c.memory_limit = "8GiB"
    g = dashboard.RepoGroup("alpha", "/repos/alpha", tmp_path / "a.yaml", [c])
    now = datetime(2026, 6, 8, 12, 0, tzinfo=UTC)
    out = _render_text(
        dashboard.render(
            [g],
            selected=None,
            now=now,
            git_enabled=True,
        )
    )
    assert "3.7G" in out  # used
    assert "8GiB" in out  # limit
    assert "MEM" in out  # the new column header
    assert "MEMORY LIMIT" not in out  # bare-limit column was swapped out


def test_dashboard_command_delegates_to_run(mocker):
    from jailbee.config import ConfigNotFoundError

    run = mocker.patch("jailbee.dashboard.run", return_value=0)
    mocker.patch("jailbee.incus.Incus")
    mocker.patch("jailbee.config.load_repo_config", side_effect=ConfigNotFoundError("none"))
    result = CliRunner().invoke(app, ["dashboard", "-i", "5", "--no-git"])
    assert result.exit_code == 0
    _, kwargs = run.call_args
    # The cadence flags no longer reach the TUI: the state service owns it.
    assert {"interval", "git_interval", "no_git"}.isdisjoint(kwargs)
    assert kwargs["cwd_root"] is None


def test_dashboard_command_passes_the_cwd_root_when_its_config_loads(mocker):
    """The probe answers "is the cwd a repo we can show", and the cwd's own
    *root* is what both dashboards now key on."""
    run = mocker.patch("jailbee.dashboard.run", return_value=0)
    mocker.patch("jailbee.incus.Incus")
    mocker.patch("jailbee.config.load_repo_config", return_value=mocker.Mock())
    result = CliRunner().invoke(app, ["dashboard"])
    assert result.exit_code == 0
    assert run.call_args.kwargs["cwd_root"] == Path.cwd()


def test_dashboard_command_passes_none_when_the_cwd_repo_config_is_broken(mocker):
    """The launch probe is a real load now, not a `find_repo_config` stat.

    A repo whose config file exists but does not parse therefore stops being
    "the cwd repo": before, the path was found and `gather_rows` was left to
    skip it, which pinned a broken repo to the top of an empty dashboard.
    """
    from jailbee.config import ConfigError

    run = mocker.patch("jailbee.dashboard.run", return_value=0)
    mocker.patch("jailbee.incus.Incus")
    mocker.patch("jailbee.config.load_repo_config", side_effect=ConfigError("bad yaml"))
    result = CliRunner().invoke(app, ["dashboard"])
    assert result.exit_code == 0
    assert run.call_args.kwargs["cwd_root"] is None


def test_dashboard_command_survives_an_unreadable_cwd_repo_config(mocker):
    """An unreadable config file must not traceback the dashboard at launch.

    The probe reads the file now, where `find_repo_config()` only stat'd it, so
    a permission error, a dangling symlink or an I/O error reaches the probe as
    a bare `OSError` — `load_config` wraps YAML and Pydantic failures as
    `ConfigError`, but never `read_text()`'s own errors. A launch-time
    traceback is the one failure a user cannot work around, so the probe treats
    it exactly like an unloadable config: this is not the cwd repo.
    """
    run = mocker.patch("jailbee.dashboard.run", return_value=0)
    mocker.patch("jailbee.incus.Incus")
    mocker.patch(
        "jailbee.config.load_repo_config",
        side_effect=PermissionError(13, "Permission denied"),
    )
    result = CliRunner().invoke(app, ["dashboard"])
    assert result.exit_code == 0
    assert run.call_args.kwargs["cwd_root"] is None


def test_remote_dashboard_never_loads_the_cwd_and_runs_restricted(mocker, monkeypatch) -> None:
    """A remote SSH session: registered repos only, no setup offer (its steps
    run on the host), and `run` told it is remote."""
    monkeypatch.setenv("JAILBEE_REMOTE_SSH", "1")
    monkeypatch.setenv("JAILBEE_SSH_EXCLUDED_REPOS", '["snapshot"]')
    load = mocker.patch("jailbee.config.load_repo_config")
    advise = mocker.patch("jailbee.cli._advise_setup")
    run = mocker.patch("jailbee.dashboard.run", return_value=0)
    mocker.patch("jailbee.incus.Incus")

    from jailbee.config.models_remote import RemoteCommandPolicy, RemoteSSHConfig

    policy_json = RemoteSSHConfig(
        exec=True,
        excluded_repos=["policy"],
        commands=RemoteCommandPolicy(mode="full"),
    ).model_dump_json()
    result = CliRunner().invoke(app, ["dashboard", "--remote-policy-json", policy_json])

    assert result.exit_code == 0
    load.assert_not_called()
    advise.assert_not_called()
    assert run.call_args.kwargs["cwd_root"] is None
    assert run.call_args.kwargs["remote"] is True
    assert run.call_args.kwargs["over_ssh"] is True
    assert run.call_args.kwargs["ssh_policy"].model_dump_json() == policy_json
    assert run.call_args.kwargs["scope"].excluded == frozenset({"snapshot", "policy"})


@pytest.mark.parametrize("policy_json", [None, "not-json"])
def test_ssh_dashboard_fails_closed_without_valid_policy(mocker, monkeypatch, policy_json):
    monkeypatch.setenv("JAILBEE_SSH_SESSION", "1")
    monkeypatch.setenv("JAILBEE_SSH_EXCLUDED_REPOS", "[]")
    run = mocker.patch("jailbee.dashboard.run")
    popen = mocker.patch("subprocess.Popen")
    argv = ["dashboard"]
    if policy_json is not None:
        argv += ["--remote-policy-json", policy_json]

    result = CliRunner().invoke(app, argv)

    assert result.exit_code == 2
    assert "policy" in result.output
    run.assert_not_called()
    popen.assert_not_called()


def test_local_dashboard_is_not_remote(mocker, monkeypatch) -> None:
    monkeypatch.delenv("JAILBEE_REMOTE_SSH", raising=False)
    mocker.patch("jailbee.config.load_repo_config", side_effect=OSError("no repo"))
    mocker.patch("jailbee.cli._advise_setup")
    run = mocker.patch("jailbee.dashboard.run", return_value=0)
    mocker.patch("jailbee.incus.Incus")

    result = CliRunner().invoke(app, ["dashboard"])

    assert result.exit_code == 0
    assert run.call_args.kwargs["remote"] is False


@pytest.mark.parametrize("argv", [["dashboard", "--gui"], ["gui"], ["gui", "--foreground"]])
def test_remote_session_never_gets_the_qt_dashboard(mocker, monkeypatch, argv) -> None:
    """A Qt window would open on the host's display, not the SSH client's."""
    monkeypatch.setenv("JAILBEE_REMOTE_SSH", "1")
    monkeypatch.setenv("JAILBEE_SSH_EXCLUDED_REPOS", "[]")
    mocker.patch("jailbee.cli._advise_setup")
    mocker.patch("jailbee.incus.Incus")
    preflight = mocker.patch("jailbee.qtui.app.preflight", return_value=[Path("/tmp/x")])
    qrun = mocker.patch("jailbee.qtui.app.run", return_value=0)
    popen = mocker.patch("subprocess.Popen")

    result = CliRunner().invoke(app, argv)

    assert result.exit_code == 2
    assert "not available over remote SSH" in result.output
    preflight.assert_not_called()
    qrun.assert_not_called()
    popen.assert_not_called()


def test_remote_merge_menu_refusal_does_not_spawn_command(mocker, tmp_path) -> None:
    from jailbee.config.models_remote import RemoteCommandPolicy, RemoteSSHConfig
    from jailbee.remote_ssh.router import RouteError

    target = dashboard.RepoTarget(tmp_path, None)
    run = mocker.patch("jailbee.dashboard.subprocess.run")
    policy = RemoteSSHConfig(commands=RemoteCommandPolicy(mode="disabled"))

    with pytest.raises(RouteError, match="disabled"):
        dashboard._dispatch_action(
            target,
            "merge",
            "alpha",
            remote=True,
            over_ssh=True,
            ssh_policy=policy,
        )

    run.assert_not_called()


@pytest.mark.parametrize(("key", "verb"), [(b"t", "tmux"), (b"s", "shell")])
def test_ssh_dashboard_existing_attach_actions_work_without_exec(
    mocker, tmp_path, key, verb
) -> None:
    from jailbee.config.models_remote import RemoteSSHConfig

    group = dashboard.RepoGroup("alpha", str(tmp_path), None, [_ci("alpha-x", "alpha")])
    child = mocker.patch.object(dashboard.subprocess, "run")
    child.return_value.returncode = 0
    _mock_terminal(mocker)
    _fake_state(mocker, [group])
    mocker.patch.object(dashboard.select, "select", return_value=([True], [], []))
    keys = itertools.chain([b"j", key, b"\x03"], itertools.repeat(b"\x03"))
    mocker.patch.object(dashboard.os, "read", side_effect=lambda fd, n: next(keys))

    assert (
        dashboard.run(
            mocker.Mock(),
            None,
            remote=True,
            over_ssh=True,
            ssh_policy=RemoteSSHConfig(),
        )
        == 0
    )
    child.assert_called_once_with(
        ["jailbee", verb, "alpha-x", "--force"], check=False, cwd=tmp_path
    )


def test_registered_only_flag_is_gone() -> None:
    """The remote form is decided by the session marker, not a flag a caller
    could forget to pass."""
    result = CliRunner().invoke(app, ["dashboard", "--registered-only"])

    assert result.exit_code == 2


def test_dashboard_command_lets_a_programming_error_out_of_the_probe(mocker):
    """The widened `except` is `(ConfigError, OSError)`, not `Exception`.

    Config problems and I/O problems both mean "this is not the cwd repo"; a
    bug in the loader means something else entirely and must still surface
    rather than silently rendering a dashboard with the cwd repo missing.
    """
    mocker.patch("jailbee.dashboard.run", return_value=0)
    mocker.patch("jailbee.incus.Incus")
    mocker.patch("jailbee.config.load_repo_config", side_effect=RuntimeError("bug"))
    result = CliRunner().invoke(app, ["dashboard"])
    assert result.exit_code != 0
    assert isinstance(result.exception, RuntimeError)


def test_tui_command_is_an_alias_for_the_dashboard(mocker):
    """`jailbee tui` mirrors `jailbee gui`: the TUI frontend, same options."""
    from jailbee.config import ConfigNotFoundError

    run = mocker.patch("jailbee.dashboard.run", return_value=0)
    mocker.patch("jailbee.incus.Incus")
    mocker.patch("jailbee.config.load_repo_config", side_effect=ConfigNotFoundError("none"))
    result = CliRunner().invoke(app, ["tui", "-i", "5", "--git-interval", "7", "--no-git"])
    assert result.exit_code == 0
    _, kwargs = run.call_args
    assert {"interval", "git_interval", "no_git"}.isdisjoint(kwargs)


def test_menu_actions_clear_job_entry_follows_session_when_clearable():
    actions = dashboard.menu_actions(_ctx(job_clearable=True))
    assert actions[:4] == [
        ("Attach tmux", "tmux"),
        ("Open shell", "shell"),
        ("Outbox", "outbox browse"),
        ("Clear failed job", "job clear"),
    ]


def test_menu_actions_no_clear_entry_when_not_clearable():
    actions = dashboard.menu_actions(_ctx())
    assert "job clear" not in [verb for _, verb in actions]


def test_menu_actions_clear_job_precedes_open_pr():
    actions = dashboard.menu_actions(_ctx(pr_number=7, job_clearable=True))
    verbs = [verb for _, verb in actions]
    assert verbs.index("job clear") < verbs.index("pr --open")


def test_menu_actions_clear_job_offered_for_a_container_with_no_state():
    # A job row whose container never existed is rendered with state "—".
    actions = dashboard.menu_actions(_ctx(state="—", job_clearable=True))
    assert actions[0] == ("Clear failed job", "job clear")


def test_actions_for_container_offers_clear_for_a_failed_job(mocker):
    from jailbee import background
    from jailbee.lifecycle import ContainerInfo

    mocker.patch.object(background, "worker_alive", return_value=True)
    c = ContainerInfo(
        name="p-foo",
        state="Running",
        network="strict",
        ip=None,
        memory_limit=None,
        repo="p",
        job_phase=background.PHASE_FAILED,
        job_pid=4242,
        job_kind="create",
    )
    groups = [dashboard.RepoGroup("p", "/repo", Path("/repo/.jailbee/config.yaml"), [c])]

    verbs = [verb for _, verb in dashboard.actions_for_container(groups, "p-foo")]

    assert verbs[:5] == ["tmux", "shell", "outbox browse", "job clear", "job log"]


def test_actions_for_container_offers_clear_for_a_dead_worker(mocker):
    from jailbee import background
    from jailbee.lifecycle import ContainerInfo

    mocker.patch.object(background, "worker_alive", return_value=False)
    c = ContainerInfo(
        name="p-foo",
        state="Running",
        network="strict",
        ip=None,
        memory_limit=None,
        repo="p",
        job_phase=background.PHASE_CLONING,
        job_pid=999,
        job_kind="create",
    )
    groups = [dashboard.RepoGroup("p", "/repo", Path("/repo/.jailbee/config.yaml"), [c])]

    verbs = [verb for _, verb in dashboard.actions_for_container(groups, "p-foo")]

    assert verbs[:5] == ["tmux", "shell", "outbox browse", "job clear", "job log"]


def test_actions_for_container_no_clear_for_a_live_job(mocker):
    from jailbee import background
    from jailbee.lifecycle import ContainerInfo

    mocker.patch.object(background, "worker_alive", return_value=True)
    c = ContainerInfo(
        name="p-foo",
        state="Running",
        network="strict",
        ip=None,
        memory_limit=None,
        repo="p",
        job_phase=background.PHASE_CLONING,
        job_pid=4242,
        job_kind="create",
    )
    groups = [dashboard.RepoGroup("p", "/repo", Path("/repo/.jailbee/config.yaml"), [c])]

    verbs = [verb for _, verb in dashboard.actions_for_container(groups, "p-foo")]

    assert "job clear" not in verbs


def test_actions_for_container_no_clear_without_a_job():
    from jailbee.lifecycle import ContainerInfo

    c = ContainerInfo(
        name="p-foo",
        state="Running",
        network="strict",
        ip=None,
        memory_limit=None,
        repo="p",
    )
    groups = [dashboard.RepoGroup("p", "/repo", Path("/repo/.jailbee/config.yaml"), [c])]

    verbs = [verb for _, verb in dashboard.actions_for_container(groups, "p-foo")]

    assert "job clear" not in verbs


def test_fold_target_works_from_a_header_and_from_a_container():
    """Resolve the group for either a header or one of its container rows."""
    groups = [dashboard.RepoGroup("a", "/a", None, [_ci("a-1", "a")])]
    assert dashboard.fold_target(groups, dashboard.Row("repo", "a")) == "a"
    assert dashboard.fold_target(groups, dashboard.Row("container", "a-1")) == "a"
    assert dashboard.fold_target(groups, dashboard.Row("container", "gone")) is None
    assert dashboard.fold_target(groups, None) is None


def test_toggle_folded_is_a_pure_set_flip():
    assert dashboard.toggle_folded(frozenset(), "a") == frozenset({"a"})
    assert dashboard.toggle_folded(frozenset({"a", "b"}), "a") == frozenset({"b"})


def test_render_marks_a_folded_group_and_hides_its_rows(tmp_path):
    now = datetime(2026, 6, 8, 12, 0, tzinfo=UTC)
    groups = [
        dashboard.RepoGroup("alpha", "/a", tmp_path / "a.yaml", [_ci("alpha-one", "alpha")]),
        dashboard.RepoGroup("beta", "/b", tmp_path / "b.yaml", [_ci("beta-two", "beta")]),
    ]
    out = _render_text(
        dashboard.render(
            groups,
            selected=None,
            now=now,
            git_enabled=True,
            folded=frozenset({"alpha"}),
        )
    )
    # NAME cells show display_name (repo prefix stripped, see ContainerInfo);
    # "beta-two" -> "two" is the neighbour's untouched row, distinct from
    # "alpha-one" -> "one" so the two containers cannot be confused for
    # each other in the assertion below.
    assert " one " not in out  # folded away; do not match MODE=clone
    assert "two" in out  # its neighbour is untouched
    assert "▸" in out and "▾" in out  # collapsed and expanded markers both drawn
    assert "1 folded" in out  # the title says what is hidden


def test_render_marks_a_selected_repo_header(tmp_path):
    """A header the cursor sits on must look selected — without moving.

    Headers became cursor stops at a point where `render` did not consult
    `selected` for them at all, so pressing Down onto a header made the
    highlight vanish. The first fix prefixed the header with the container
    rows' then `▸` gutter arrow, but a header has no gutter cell of its own,
    so the whole line jumped two cells right whenever the cursor landed on
    it. Every cursor row is now marked by the highlight alone: same text,
    same position, same style for a header as for a container row.
    """
    g = dashboard.RepoGroup("alpha", "/a", tmp_path / "a.yaml", [_ci("alpha-one", "alpha")])
    kwargs = dict(
        now=datetime(2026, 6, 8, 12, 0, tzinfo=UTC),
        git_enabled=True,
    )

    unselected = _render_text(dashboard.render([g], selected=None, **kwargs))
    on_header = _render_text(
        dashboard.render([g], selected=dashboard.Row("repo", "alpha"), **kwargs)
    )
    # The plain text is identical: nothing is inserted, so nothing shifts.
    assert unselected == on_header

    plain = dashboard.repo_heading(g, None, frozenset())
    selected = dashboard.repo_heading(g, dashboard.Row("repo", "alpha"), frozenset())
    assert selected.plain == plain.plain
    assert str(plain.style) == "bold cyan"
    # Exactly the style a selected container row gets from `repo_table`.
    row = dashboard.repo_table(g, [], (), dashboard.Row("container", "alpha-one"))
    assert str(selected.style) == str(row.rows[0].style) == dashboard.CURSOR_STYLE


def test_cursor_style_is_distinct_from_every_heading_colour(tmp_path):
    """The cursor must never look like a heading's resting colour, or the
    cursor on that heading would be invisible."""
    repo = dashboard.RepoGroup("alpha", "/a", None, [])
    orphan = dashboard.RepoGroup("gamma", None, None, [])
    resting = {str(dashboard.repo_heading(g, None, frozenset()).style) for g in (repo, orphan)}
    assert resting == {"bold cyan", "bold yellow"}
    assert dashboard.CURSOR_STYLE == "bold magenta"
    for g in (repo, orphan):
        on_it = dashboard.repo_heading(g, dashboard.Row("repo", g.prefix), frozenset())
        assert str(on_it.style) == dashboard.CURSOR_STYLE


def test_render_gutter_lands_on_the_first_enabled_column_not_just_name(tmp_path):
    """`render` used to give the group-header row's first cell a 2-char
    gutter unconditionally, but a container row's first cell only got one
    when `f.name == "name"`. With `name` disabled via the settings overlay
    and some other field first, the header ends up indented two columns to
    the right of its own container rows — this is now reachable since
    disabling `name` from the settings UI is a real, supported action.
    Fails against the old `f.name == "name"` gating, which never puts a
    gutter on the container row here (its first field is `state`, not
    `name`) while the header row still gets one unconditionally."""
    now = datetime(2026, 6, 8, 12, 0, tzinfo=UTC)
    g = dashboard.RepoGroup("alpha", "/a", tmp_path / "a.yaml", [_ci("alpha-one", "alpha")])
    out = _render_text(
        dashboard.render(
            [g],
            selected=None,
            now=now,
            git_enabled=True,
            enabled=("state", "network"),  # `name` disabled; `state` is first
        )
    )
    lines = [ln for ln in out.splitlines() if ln.strip()]
    header_line = next(ln for ln in lines if "alpha" in ln)
    data_line = next(ln for ln in lines if "▶" in ln)
    # Both lines start with the Panel's own border+padding ("│ "), identical
    # on every row, so strip exactly that one border character before
    # measuring each row's own indentation — comparing the raw lines
    # (border included) would always read indent 0 for both, since neither
    # starts with a literal space.
    header_indent = len(header_line[1:]) - len(header_line[1:].lstrip(" "))
    data_indent = len(data_line[1:]) - len(data_line[1:].lstrip(" "))
    assert header_indent < data_indent


def test_render_counts_every_container_even_when_folded(tmp_path):
    """The title is a census, not a row count: a folded repo's containers are
    still there and still running."""
    now = datetime(2026, 6, 8, 12, 0, tzinfo=UTC)
    groups = [
        dashboard.RepoGroup(
            "alpha",
            "/a",
            tmp_path / "a.yaml",
            [_ci("alpha-one", "alpha"), _ci("alpha-two", "alpha")],
        )
    ]
    out = _render_text(
        dashboard.render(
            groups,
            selected=None,
            now=now,
            git_enabled=True,
            folded=frozenset({"alpha"}),
        )
    )
    assert "2 containers" in out


def test_folded_groups_retain_enabled_conditional_columns(tmp_path):
    """Folding data away must not move the remaining columns."""
    now = datetime(2026, 6, 8, 12, 0, tzinfo=UTC)
    with_pr = _ci("alpha-one", "alpha")
    with_pr.pr_number = 7
    groups = [
        dashboard.RepoGroup("alpha", "/a", tmp_path / "a.yaml", [with_pr]),
        dashboard.RepoGroup("beta", "/b", tmp_path / "b.yaml", [_ci("beta-one", "beta")]),
    ]
    kwargs = dict(
        selected=None,
        now=now,
        git_enabled=True,
        shown_columns=dashboard.nonempty_columns(groups, now=now),
    )
    unfolded = _render_text(dashboard.render(groups, **kwargs))
    folded = _render_text(dashboard.render(groups, folded=frozenset({"alpha"}), **kwargs))

    assert "PR" in unfolded
    assert "PR" in folded


def test_space_key_is_fold_key_and_enter_remains_bound():
    assert dashboard.parse_key(b" ") == "space"
    binding = dashboard.binding_for_token("space")
    assert binding is not None
    assert binding.hint and binding.label
    assert dashboard.parse_key(b"\r") == "enter"


def test_space_is_the_fold_key_and_still_toggles_settings_binding():
    assert dashboard.parse_key(b" ") == "space"
    binding = dashboard.binding_for_token("space")
    assert binding is not None
    assert "fold" in binding.label and "Settings" in binding.label


def test_space_folds_then_unfolds_the_selected_repo(mocker, tmp_path):
    group = dashboard.RepoGroup("alpha", str(tmp_path), None, [_ci("alpha-x", "alpha")])
    save = mocker.patch.object(dashboard, "save_view_state")

    assert _drive_run(mocker, [b" ", b" "], [group]) == 0

    folded = [c.args[2].folded for c in save.call_args_list]
    assert folded == [frozenset({"alpha"}), frozenset()]


def test_space_on_a_container_row_folds_its_repo_and_selects_the_header(mocker, tmp_path):
    group = dashboard.RepoGroup("alpha", str(tmp_path), None, [_ci("alpha-x", "alpha")])
    save = mocker.patch.object(dashboard, "save_view_state")
    render = mocker.patch.object(dashboard, "render", wraps=dashboard.render)

    _drive_run(mocker, [b"j", b" "], [group])

    assert save.call_args.args[2].folded == frozenset({"alpha"})
    assert render.call_args_list[-1].args[1] == dashboard.Row("repo", "alpha")


def test_space_with_nothing_selected_does_nothing(mocker):
    save = mocker.patch.object(dashboard, "save_view_state")
    assert _drive_run(mocker, [b" "]) == 0
    save.assert_not_called()


def test_run_space_only_persists_when_settings_overlay_is_open(mocker):
    save = mocker.patch.object(dashboard, "save_view_state")
    assert _drive_run(mocker, [b" ", b"S", b" "]) == 0
    save.assert_called_once()


def test_settings_key_is_bound_to_f2_and_shift_s():
    """Both F2 encodings, because terminals disagree, plus a letter that works
    everywhere. `s` is already shell, so the alias is `S`."""
    assert dashboard.parse_key(b"\x1bOQ") == "settings"
    assert dashboard.parse_key(b"\x1b[12~") == "settings"
    assert dashboard.parse_key(b"S") == "settings"
    binding = dashboard.binding_for_token("settings")
    assert binding is not None and binding.hint


def test_all_column_names_is_the_full_ls_vocabulary():
    """The Fields tab offers every real column, including ones off by default
    in both views — that is the point of an enabled set over a hide list."""
    from datetime import UTC, datetime

    from jailbee.lifecycle import ls_field_specs

    names = dashboard.all_column_names()
    expected = [f.name for f in ls_field_specs(now=datetime(2026, 6, 8, tzinfo=UTC))]
    assert list(names) == expected
    assert "full_name" in names and "git_status" in names and "ip" in names


def test_dynamic_column_names_are_exactly_the_show_if_ones():
    assert dashboard.dynamic_column_names() == frozenset(
        {"job", "ttl", "pr", "issues", "mode", "group"}
    )


def test_settings_repo_prefixes_keeps_a_folded_repo_that_is_not_on_screen():
    """Otherwise a repo whose containers are gone could never be unfolded:
    it draws no group, so the Repos tab would not list it."""
    groups = [
        dashboard.RepoGroup("alpha", "/a", None, [_ci("alpha-one", "alpha")]),
        dashboard.RepoGroup("empty", "/e", None, []),
    ]
    prefixes = dashboard.settings_repo_prefixes(groups, frozenset({"alpha", "vanished"}))

    assert "alpha" in prefixes
    assert "vanished" in prefixes  # folded but absent — still reachable
    assert "empty" in prefixes
    assert len(prefixes) == len(set(prefixes))  # no duplicate for a folded on-screen repo


def test_render_draws_the_settings_overlay_below_the_table(tmp_path):
    from jailbee.dashboard_settings import open_settings

    now = datetime(2026, 6, 8, 12, 0, tzinfo=UTC)
    g = dashboard.RepoGroup("alpha", "/a", tmp_path / "a.yaml", [_ci("alpha-one", "alpha")])
    overlay = open_settings(
        field_names=dashboard.all_column_names(),
        enabled=frozenset(dashboard.default_columns()),
        repo_prefixes=("alpha",),
        folded=frozenset(),
    )
    out = _render_text(
        dashboard.render(
            [g],
            selected=None,
            now=now,
            git_enabled=True,
            overlay=overlay,
        )
    )
    # The live table stays on screen behind the panel — that is the whole
    # reason the overlay is a panel and not a full-screen modal.
    # (The container row renders as "one": display_name strips the repo
    # prefix, same as the menu-overlay table-visibility check above.)
    assert "one" in out
    assert "settings" in out
    assert "Fields" in out


# ---------------------------------------------------------------------------
# run() loop: a fake terminal, driven by a scripted key sequence
# ---------------------------------------------------------------------------


class _SyncJobs(JobRunner):
    """`JobRunner` that runs the child through the (mocked) `subprocess.run`.

    The real runner spawns a `Popen` and waits on a thread; patching `Popen`
    is process-wide and breaks every other `subprocess.run`, so these tests
    keep asserting on the one `subprocess.run` mock and get the result on the
    next `poll()`, exactly as the real runner delivers it. The mock's
    `returncode` is the child's exit code; stderr is the mock's `stderr` when it
    is a string, else empty.
    """

    def start(self, key, label, argv, cwd, on_done):
        proc = dashboard.subprocess.run(argv, check=False, cwd=cwd)
        self._labels[key] = label
        stderr = proc.stderr if isinstance(proc.stderr, str) else ""
        self._finished.append((key, on_done, JobResult(proc.returncode, stderr)))


def _mock_terminal(mocker):
    """Patch everything ``run()`` touches on a real terminal and the state DB.

    Returns the ``tty.setcbreak`` mock, which is this file's marker for "the
    screen has been taken": it is the first thing `run()` does to the terminal
    before `Live` starts, so anything recorded while its ``call_count`` is 0
    happened while the user could still see their own shell.
    """
    mocker.patch.object(dashboard, "collect_repo_roots", return_value=[Path("/x")])
    mocker.patch("jailbee.db.get_engine", return_value=mocker.Mock())
    mocker.patch.object(dashboard, "seed_view_state", return_value=dashboard.ViewState())
    mocker.patch.object(dashboard, "JobRunner", _SyncJobs)

    mock_stdin = mocker.Mock()
    mock_stdin.isatty.return_value = True
    mock_stdin.fileno.return_value = 0
    mocker.patch.object(dashboard.sys, "stdin", mock_stdin)
    mock_stdout = mocker.Mock()
    mock_stdout.isatty.return_value = True
    mocker.patch.object(dashboard.sys, "stdout", mock_stdout)

    mocker.patch.object(dashboard.termios, "tcgetattr", return_value=object())
    mocker.patch.object(dashboard.termios, "tcsetattr")
    return mocker.patch.object(dashboard.tty, "setcbreak")


class FakeStateClient:
    """Stands in for `StateClient`: `latest()` returns the *same* groups list
    every frame, so a test that mutates it changes what the next frame sees."""

    def __init__(self, groups, *, git_enabled=False, status=None, fail=None):
        self.groups = groups
        self.git_enabled = git_enabled
        self._status = status
        self.fail = fail
        self.events: list[tuple] = []
        self.closed = False

    def wait_first_snapshot(self, timeout):
        self.events.append(("wait",))
        if self.fail is not None:
            raise self.fail
        return self.latest()

    def latest(self):
        return Snapshot(1, datetime(2026, 10, 4, tzinfo=UTC), self.git_enabled, self.groups)

    def status(self):
        return self._status

    def refresh(self):
        self.events.append(("refresh",))

    def set_active(self, value):
        self.events.append(("active", value))

    def close(self):
        self.closed = True


def _fake_state(mocker, groups, **kw) -> FakeStateClient:
    fake = FakeStateClient(groups, **kw)
    mocker.patch.object(dashboard, "open_state_client", return_value=fake)
    return fake


def _drive_run(
    mocker,
    key_sequence: list[bytes],
    groups: list[dashboard.RepoGroup] | None = None,
    *,
    remote: bool = False,
    over_ssh: bool = False,
    ssh_policy=None,
    view_state: dashboard.ViewState | None = None,
    git_enabled: bool = False,
) -> int:
    """Run the real ``dashboard.run()`` key loop with a fake terminal.

    Feeds ``key_sequence`` one key per main-loop iteration (``os.read`` is
    mocked, not stdin itself), padded with a trailing Ctrl-C so the loop
    always terminates even if a test's own key list doesn't. Everything
    that would touch a real terminal, the state DB, or Incus is mocked;
    the state service is a `FakeStateClient` serving ``groups`` (empty by
    default), so most tests exercise overlay and persistence behaviour with
    nothing selectable. A test that needs a real, dispatchable container
    passes its own ``groups``.
    """
    _mock_terminal(mocker)
    if view_state is not None:
        mocker.patch.object(dashboard, "seed_view_state", return_value=view_state)
    _fake_state(mocker, groups or [], git_enabled=git_enabled)
    mocker.patch.object(dashboard.select, "select", return_value=([True], [], []))

    padded = itertools.chain(key_sequence, [b"\x03"], itertools.repeat(b"\x03"))
    mocker.patch.object(dashboard.os, "read", side_effect=lambda fd, n: next(padded))

    return dashboard.run(
        mocker.Mock(),
        None,
        remote=remote,
        over_ssh=over_ssh,
        ssh_policy=ssh_policy,
    )


@pytest.mark.parametrize("git_enabled", [True, False])
def test_run_renders_with_the_snapshots_git_enabled(mocker, git_enabled):
    """The TUI forwards the service's `git_enabled` (it never probes git itself)."""
    render = mocker.patch.object(dashboard, "render", wraps=dashboard.render)
    _drive_run(mocker, [], git_enabled=git_enabled)
    assert render.call_args_list
    assert all(c.kwargs["git_enabled"] is git_enabled for c in render.call_args_list)


def _drive_run_with_reader(mocker, read, groups: list[dashboard.RepoGroup]) -> int:
    """``_drive_run`` with a caller-supplied ``os.read`` side effect.

    The fake state client serves the ``groups`` list object itself on every
    frame, and the real `present` runs on it each frame, so a reader that
    mutates it changes what the key loop sees on its next iteration.
    """
    _mock_terminal(mocker)
    _fake_state(mocker, groups)
    mocker.patch.object(dashboard.select, "select", return_value=([True], [], []))
    mocker.patch.object(dashboard.os, "read", side_effect=read)
    return dashboard.run(mocker.Mock(), None)


def _keys(text: str) -> list[bytes]:
    """One terminal read per character — how a typist feeds the prompt."""
    return [ch.encode() for ch in text]


_ENTER = b"\r"
_ESC = b"\x1b"


def test_run_enters_pr_submenu_and_dispatches_leaf(mocker, tmp_path):
    group = dashboard.RepoGroup(
        "alpha", str(tmp_path), None, [_ci("alpha-x", "alpha", pr_number=7)]
    )
    child = mocker.patch.object(dashboard.subprocess, "run")
    child.return_value.returncode = 0
    mocker.patch.object(dashboard, "_wait_for_return")
    render = mocker.patch.object(dashboard, "render", wraps=dashboard.render)

    rc = _drive_run(
        mocker,
        [b"j", b"\r", b"\x1b[B", b"\x1b[B", b"\x1b[B", b"\r", b"\x1b[B", b"\r"],
        groups=[group],
    )

    assert rc == 0
    assert any(
        isinstance(call.kwargs.get("overlay"), dashboard.MenuState)
        and call.kwargs["overlay"].active_group == "PR →"
        for call in render.call_args_list
    )
    assert any(call.args[0] == ["jailbee", "pr", "alpha-x"] for call in child.call_args_list)


def test_run_menu_hotkeys_open_a_group_and_dispatch_its_leaf(mocker, tmp_path):
    group = dashboard.RepoGroup("alpha", str(tmp_path), None, [_ci("alpha-x", "alpha")])
    child = mocker.patch.object(dashboard.subprocess, "run")
    child.return_value.returncode = 0
    mocker.patch.object(dashboard, "_wait_for_return")

    assert _drive_run(mocker, [b"j", _ENTER, b"g", b"u"], groups=[group]) == 0

    assert any(
        call.args[0] == ["jailbee", "git", "push", "alpha-x"] for call in child.call_args_list
    )


def test_run_menu_capital_hotkey_destroys_only_through_lifecycle(mocker, tmp_path):
    """`D` inside `Lifecycle →` reaches destroy; a lowercase `d` there does nothing."""
    group = dashboard.RepoGroup("alpha", str(tmp_path), None, [_ci("alpha-x", "alpha")])
    child = mocker.patch.object(dashboard.subprocess, "run")
    child.return_value.returncode = 0
    mocker.patch.object(dashboard, "_wait_for_return")

    _drive_run(mocker, [b"j", _ENTER, b"l", b"d"], groups=[group])
    assert not any("destroy" in call.args[0] for call in child.call_args_list)

    _drive_run(mocker, [b"j", _ENTER, b"l", b"D"], groups=[group])
    assert any(call.args[0][:2] == ["jailbee", "destroy"] for call in child.call_args_list)


def test_run_menu_unknown_key_leaves_the_menu_untouched(mocker, tmp_path):
    group = dashboard.RepoGroup("alpha", str(tmp_path), None, [_ci("alpha-x", "alpha")])
    child = mocker.patch.object(dashboard.subprocess, "run")
    render = mocker.patch.object(dashboard, "render", wraps=dashboard.render)

    _drive_run(mocker, [b"j", _ENTER, b"z"], groups=[group])

    menus = [c.kwargs["overlay"] for c in render.call_args_list if c.kwargs.get("overlay")]
    assert menus and isinstance(menus[-1], dashboard.MenuState) and menus[-1].index == 0
    child.assert_not_called()


def test_run_escape_backs_out_but_q_closes_submenu(mocker, tmp_path):
    group = dashboard.RepoGroup(
        "alpha", str(tmp_path), None, [_ci("alpha-x", "alpha", pr_number=7)]
    )
    render = mocker.patch.object(dashboard, "render", wraps=dashboard.render)
    child = mocker.patch.object(dashboard.subprocess, "run")

    _drive_run(
        mocker,
        [b"j", b"\r", b"\x1b[B", b"\x1b[B", b"\x1b[B", b"\r", b"\x1b", b"\r", b"q"],
        groups=[group],
    )

    overlays = [call.kwargs.get("overlay") for call in render.call_args_list]
    menus = [item for item in overlays if isinstance(item, dashboard.MenuState)]
    assert [menu.active_group for menu in menus] == [
        None,
        None,
        None,
        None,
        "PR →",
        None,
        "PR →",
    ]
    assert menus[5].index == 3
    assert overlays[-1] is None
    child.assert_not_called()


def test_run_vanished_container_closes_submenu(mocker, tmp_path):
    group = dashboard.RepoGroup(
        "alpha", str(tmp_path), None, [_ci("alpha-x", "alpha", pr_number=7)]
    )
    render = mocker.patch.object(dashboard, "render", wraps=dashboard.render)
    _mock_terminal(mocker)
    _fake_state(mocker, [group])
    turns = 0

    def ready(*args, **kwargs):
        nonlocal turns
        turns += 1
        if turns == 7:
            group.containers.clear()
        return ([True], [], [])

    mocker.patch.object(dashboard.select, "select", side_effect=ready)
    keys = itertools.chain(
        [b"j", b"\r", b"\x1b[B", b"\x1b[B", b"\x1b[B", b"\r", b"\x1b[B", b"\x03"],
        itertools.repeat(b"\x03"),
    )
    mocker.patch.object(dashboard.os, "read", side_effect=lambda fd, n: next(keys))

    assert dashboard.run(mocker.Mock(), None) == 0
    assert any(
        isinstance(call.kwargs.get("overlay"), dashboard.MenuState)
        and call.kwargs["overlay"].active_group == "PR →"
        for call in render.call_args_list
    )
    assert any("menu closed" in str(call.kwargs.get("notice")) for call in render.call_args_list)


def test_ssh_disabled_policy_rejects_new_before_prompt_or_spawn(mocker, tmp_path):
    from jailbee.config.models_remote import RemoteCommandPolicy, RemoteSSHConfig

    group = dashboard.RepoGroup("alpha", str(tmp_path), None, [_ci("alpha-x", "alpha")])
    prompt = mocker.patch("typer.prompt")
    child = mocker.patch.object(dashboard.subprocess, "run")
    render = mocker.patch.object(dashboard, "render", wraps=dashboard.render)
    policy = RemoteSSHConfig(commands=RemoteCommandPolicy(mode="disabled"))

    _drive_run(
        mocker,
        [b"n", *_keys("feature"), _ENTER, _ENTER],
        groups=[group],
        remote=True,
        over_ssh=True,
        ssh_policy=policy,
    )

    prompt.assert_not_called()
    child.assert_not_called()
    assert any("disabled" in str(call.kwargs.get("notice")) for call in render.call_args_list)
    # rejected before the first question: no prompt was ever drawn
    assert not any(
        isinstance(call.kwargs.get("overlay"), dashboard.TextPrompt)
        for call in render.call_args_list
    )


def test_ssh_allowlisted_new_prompts_then_spawns_final_argv(mocker, tmp_path):
    from jailbee.config.models_remote import RemoteCommandPolicy, RemoteSSHConfig

    group = dashboard.RepoGroup("alpha", str(tmp_path), None, [_ci("alpha-x", "alpha")])
    prompt = mocker.patch("typer.prompt")
    child = mocker.patch.object(dashboard.subprocess, "run")
    child.return_value.returncode = 0
    mocker.patch.object(dashboard, "new_container_base_default", return_value="main")
    mocker.patch.object(dashboard, "_wait_for_return")
    policy = RemoteSSHConfig(commands=RemoteCommandPolicy(mode="allowlist", allow=["new"]))

    _drive_run(
        mocker,
        [b"n", *_keys("feature"), _ENTER, _ENTER],
        groups=[group],
        remote=True,
        over_ssh=True,
        ssh_policy=policy,
    )

    prompt.assert_not_called()
    child.assert_called_once_with(
        ["jailbee", "new", "--background", "--", "feature", "main"], check=False, cwd=tmp_path
    )


def test_ssh_inline_shell_works_when_exec_entrypoint_is_disabled(mocker, tmp_path):
    from jailbee.config.models_remote import RemoteCommandPolicy, RemoteSSHConfig

    group = dashboard.RepoGroup("alpha", str(tmp_path), None, [_ci("alpha-x", "alpha")])
    child = mocker.patch.object(dashboard.subprocess, "run")
    child.return_value.returncode = 0
    policy = RemoteSSHConfig(
        exec=False, commands=RemoteCommandPolicy(mode="allowlist", allow=["shell"])
    )

    _drive_run(
        mocker,
        [b"j", b"!", b"shell", b"\r"],
        groups=[group],
        remote=True,
        over_ssh=True,
        ssh_policy=policy,
    )

    child.assert_called_once_with(["jailbee", "shell", "alpha-x"], cwd=tmp_path, check=False)


@pytest.mark.parametrize("change", ["policy", "eligibility"])
def test_open_menu_rechecks_policy_and_eligibility_before_dispatch(mocker, tmp_path, change):
    from jailbee.config.models_remote import RemoteCommandPolicy, RemoteSSHConfig

    container = _ci("alpha-x", "alpha")
    group = dashboard.RepoGroup("alpha", str(tmp_path), None, [container])
    child = mocker.patch.object(dashboard.subprocess, "run")
    render = mocker.patch.object(dashboard, "render", wraps=dashboard.render)
    _mock_terminal(mocker)
    _fake_state(mocker, [group])
    select_count = 0

    def select(*args, **kwargs):
        nonlocal select_count
        select_count += 1
        if select_count == 3:
            if change == "policy":
                policy.commands.allow[:] = ["git merge"]
            else:
                container.state = "Stopped"
        return ([True], [], [])

    mocker.patch.object(dashboard.select, "select", side_effect=select)
    keys = itertools.chain([b"j", b"\r", b"\r", b"\x03"], itertools.repeat(b"\x03"))
    mocker.patch.object(dashboard.os, "read", side_effect=lambda fd, n: next(keys))
    policy = RemoteSSHConfig(
        commands=RemoteCommandPolicy(mode="allowlist", allow=["tmux", "shell"])
    )

    dashboard.run(
        mocker.Mock(),
        None,
        remote=True,
        over_ssh=True,
        ssh_policy=policy,
    )

    assert not any(call.args[0][0] == "jailbee" for call in child.call_args_list)
    assert any(
        notice and ("not allowed" in notice or "no longer available" in notice)
        for notice in (call.kwargs.get("notice") for call in render.call_args_list)
    )


def test_run_waits_for_the_first_snapshot_before_taking_the_screen(mocker):
    """The dashboard must not show an empty table while it works out what to
    show: it waits for the state service's first snapshot before `Live` takes
    the screen, so the first frame is already populated."""
    setcbreak = _mock_terminal(mocker)
    fake = _fake_state(mocker, [])
    waited_at: list[int] = []
    wait = fake.wait_first_snapshot

    def _wait(timeout):
        waited_at.append(setcbreak.call_count)
        return wait(timeout)

    fake.wait_first_snapshot = _wait
    mocker.patch.object(dashboard.select, "select", return_value=([True], [], []))
    mocker.patch.object(dashboard.os, "read", return_value=b"\x03")

    assert dashboard.run(mocker.Mock(), None) == 0
    assert waited_at == [0]


def test_run_reports_a_failed_first_gather_without_taking_the_screen(mocker):
    """An unreachable state service is reported on the user's own terminal:
    taking the alternate screen only to hand it straight back is a worse way
    to say so."""
    from jailbee.state_service import StateServiceUnavailable

    setcbreak = _mock_terminal(mocker)
    fake = _fake_state(mocker, [], fail=StateServiceUnavailable("no server"))
    mocker.patch.object(dashboard.select, "select", return_value=([True], [], []))
    mocker.patch.object(dashboard.os, "read", return_value=b"\x03")

    assert dashboard.run(mocker.Mock(), None) == 1
    assert setcbreak.call_count == 0
    assert fake.closed


def test_run_degrades_when_save_view_state_fails(mocker):
    """A DB write failure on the keypress path (Space in settings, Fold in a repo menu,
    the settings overlay toggle) must not crash the session.

    Before this branch the TUI never wrote to the DB at all, so a failing
    write is a new failure mode: ``run()``'s own ``try`` only catches
    ``KeyboardInterrupt``, so an unguarded ``save_view_state`` propagating
    a ``database is locked`` (or any other write failure) would end the
    whole dashboard with a traceback. Fails if ``persist_view_state``'s
    try/except is removed and the exception is left to propagate.
    """
    save = mocker.patch.object(
        dashboard, "save_view_state", side_effect=OSError("database is locked")
    )
    # "S" opens the settings overlay, Space toggles the field under the
    # cursor on the Fields tab — one of the three
    # persist_view_state call sites, reached with no live groups at all.
    rc = _drive_run(mocker, [b"S", b" "])

    assert rc == 0  # run() returned normally, no exception propagated
    save.assert_called_once()  # the write was attempted, and it failed


def test_run_persists_view_state_when_the_write_succeeds(mocker):
    """Sanity check for the harness itself: the same key sequence, without
    a failing ``save_view_state``, writes through normally."""
    from jailbee.db.view_prefs import FRONTEND_TUI

    save = mocker.patch.object(dashboard, "save_view_state")
    rc = _drive_run(mocker, [b"S", b" "])

    assert rc == 0
    save.assert_called_once()
    _engine, frontend, state = save.call_args.args
    assert frontend == FRONTEND_TUI
    assert isinstance(state, dashboard.ViewState)


def test_run_visibility_tab_uses_raw_prefixes_and_persists_complete_state(mocker, tmp_path):
    from jailbee.dashboard_settings import SettingsState

    alpha = dashboard.RepoGroup("alpha", str(tmp_path / "a"), None, [_ci("alpha-one", "alpha")])
    empty = dashboard.RepoGroup("empty", str(tmp_path / "e"), None, [])
    save = mocker.patch.object(dashboard, "save_view_state")
    render = mocker.patch.object(dashboard, "render", wraps=dashboard.render)

    assert (
        _drive_run(
            mocker,
            [b"S", b"\t", b"\t", b"\x1b[B", b" "],
            [alpha, empty],
            view_state=dashboard.ViewState(("name",), frozenset({"vanished"})),
        )
        == 0
    )

    overlays = [call.kwargs.get("overlay") for call in render.call_args_list]
    visibility = next(
        overlay
        for overlay in overlays
        if isinstance(overlay, SettingsState) and overlay.tab == "visibility"
    )
    assert visibility.visibility_repo_prefixes == ("alpha", "empty")
    state = save.call_args.args[2]
    assert state.columns == ("name",)
    assert state.folded == frozenset({"vanished"})
    assert state.hidden_repos == frozenset({"alpha"})


def test_run_new_from_empty_repo_header_dispatches_to_repo_root(mocker, tmp_path):
    group = dashboard.RepoGroup("empty", str(tmp_path), None, [])
    prompt = mocker.patch("typer.prompt")
    child = mocker.patch.object(dashboard.subprocess, "run")
    child.return_value.returncode = 0
    mocker.patch.object(dashboard, "new_container_base_default", return_value="main")
    mocker.patch.object(dashboard, "_wait_for_return")

    keys = [b"n", *_keys("feature"), _ENTER, _ENTER]  # base prefilled with "main"
    assert _drive_run(mocker, keys, [group]) == 0

    prompt.assert_not_called()  # the terminal is never handed over for a question
    child.assert_called_once_with(
        ["jailbee", "new", "--background", "--", "feature", "main"], check=False, cwd=tmp_path
    )


def test_run_new_prompt_is_drawn_in_the_frame_and_keeps_the_table(mocker, tmp_path):
    group = dashboard.RepoGroup("alpha", str(tmp_path), None, [_ci("alpha-x", "alpha")])
    render = mocker.patch.object(dashboard, "render", wraps=dashboard.render)
    mocker.patch.object(dashboard, "new_container_base_default", return_value="main")

    _drive_run(mocker, [b"n", *_keys("fe")], [group])

    prompts = [
        c.kwargs["overlay"]
        for c in render.call_args_list
        if isinstance(c.kwargs.get("overlay"), dashboard.TextPrompt)
    ]
    assert prompts[-1].label == "New branch"
    assert prompts[-1].text == "fe"
    # the table is still drawn behind the prompt, the cursor where `n` was pressed
    last = next(
        c for c in reversed(render.call_args_list) if c.kwargs.get("overlay") is prompts[-1]
    )
    assert last.args[0] == [group]
    assert last.args[1] == dashboard.Row("repo", "alpha")


def test_run_new_escape_at_either_step_spawns_nothing_and_keeps_the_dashboard(mocker, tmp_path):
    group = dashboard.RepoGroup("alpha", str(tmp_path), None, [_ci("alpha-x", "alpha")])
    child = mocker.patch.object(dashboard.subprocess, "run")
    mocker.patch.object(dashboard, "new_container_base_default", return_value="main")
    render = mocker.patch.object(dashboard, "render", wraps=dashboard.render)

    for keys in (
        [b"n", _ESC],
        [b"n", *_keys("feature"), _ENTER, _ESC],
        [b"n", *_keys("feature"), _ENTER, b"\x03"],  # Ctrl-C answers the prompt only
    ):
        child.reset_mock()
        render.reset_mock()
        assert _drive_run(mocker, [*keys, b"h", _ESC], [group]) == 0
        child.assert_not_called()
        # after cancelling, the dashboard still handled a later key (help opened)
        overlays = [c.kwargs.get("overlay") for c in render.call_args_list]
        assert "help" in overlays
        assert any("Cancelled" in str(c.kwargs.get("notice")) for c in render.call_args_list)


def test_run_new_blank_branch_is_rejected_inline_not_dispatched(mocker, tmp_path):
    group = dashboard.RepoGroup("alpha", str(tmp_path), None, [_ci("alpha-x", "alpha")])
    child = mocker.patch.object(dashboard.subprocess, "run")
    mocker.patch.object(dashboard, "new_container_base_default", return_value="main")
    render = mocker.patch.object(dashboard, "render", wraps=dashboard.render)

    _drive_run(mocker, [b"n", *_keys("  "), _ENTER], [group])

    child.assert_not_called()
    assert any(
        isinstance(c.kwargs.get("overlay"), dashboard.TextPrompt)
        and c.kwargs["overlay"].error == "New branch cannot be empty"
        for c in render.call_args_list
    )


def test_run_new_trims_answers_and_rejects_a_blank_base_inline(mocker, tmp_path):
    group = dashboard.RepoGroup("alpha", str(tmp_path), None, [_ci("alpha-x", "alpha")])
    child = mocker.patch.object(dashboard.subprocess, "run")
    child.return_value.returncode = 0
    mocker.patch.object(dashboard, "new_container_base_default", return_value="main")
    mocker.patch.object(dashboard, "_wait_for_return")
    render = mocker.patch.object(dashboard, "render", wraps=dashboard.render)
    wipe_main = [b"\x7f"] * 4

    keys = [
        b"n",
        *_keys("  feature  "),
        _ENTER,
        *wipe_main,
        *_keys("  "),
        _ENTER,  # a whitespace-only base: rejected inline
        *[b"\x7f"] * 2,
        *_keys(" dev "),
        _ENTER,
    ]
    assert _drive_run(mocker, keys, [group]) == 0

    assert any(
        isinstance(c.kwargs.get("overlay"), dashboard.TextPrompt)
        and c.kwargs["overlay"].error == "Base branch cannot be empty"
        for c in render.call_args_list
    )
    child.assert_called_once_with(
        ["jailbee", "new", "--background", "--", "feature", "dev"], check=False, cwd=tmp_path
    )


def _retarget_group(tmp_path):
    info = dataclasses.replace(_ci("alpha-x", "alpha"), base_branch="feat/a")
    return dashboard.RepoGroup("alpha", str(tmp_path), None, [info])


def _fake_branches(_root, *, exclude=None):
    return tuple(b for b in ("main", "feat/a", "develop") if b != exclude)


def _text_prompts(render):
    return [
        c.kwargs["overlay"]
        for c in render.call_args_list
        if isinstance(c.kwargs.get("overlay"), dashboard.TextPrompt)
    ]


def test_run_retarget_asks_inline_and_runs_the_cli_with_the_base(mocker, tmp_path):
    group = _retarget_group(tmp_path)
    child = mocker.patch.object(dashboard.subprocess, "run")
    child.return_value.returncode = 0
    mocker.patch.object(dashboard, "_wait_for_return")
    mocker.patch.object(dashboard, "host_branches", side_effect=_fake_branches)
    render = mocker.patch.object(dashboard, "render", wraps=dashboard.render)

    keys = [b"j", _ENTER, b"g", b"b", *_keys("dev"), b"\t", _ENTER]
    assert _drive_run(mocker, keys, [group]) == 0

    prompts = _text_prompts(render)
    assert prompts[0].purpose == "container-retarget"
    assert prompts[0].suggestions == ("main", "develop")  # current base left out
    assert prompts[0].require_suggestion is True
    assert "feat/a" in prompts[0].title
    child.assert_called_once()
    assert child.call_args.args[0] == ["jailbee", "git", "retarget", "--", "alpha-x", "develop"]


def test_run_retarget_refuses_an_unknown_branch_inline(mocker, tmp_path):
    group = _retarget_group(tmp_path)
    child = mocker.patch.object(dashboard.subprocess, "run")
    mocker.patch.object(dashboard, "host_branches", side_effect=_fake_branches)
    render = mocker.patch.object(dashboard, "render", wraps=dashboard.render)

    _drive_run(mocker, [b"j", _ENTER, b"g", b"b", *_keys("nope"), _ENTER], [group])

    child.assert_not_called()
    assert any(p.error == "'nope' is not one of the listed branches" for p in _text_prompts(render))


def test_run_retarget_prompt_is_gated_by_the_dispatch_prechecks(mocker, tmp_path):
    """No prompt opens when the pre-checks (SSH policy, availability) refuse the verb."""
    group = _retarget_group(tmp_path)
    child = mocker.patch.object(dashboard.subprocess, "run")
    mocker.patch.object(dashboard, "host_branches", side_effect=_fake_branches)
    mocker.patch.object(
        dashboard, "check_dashboard_command", side_effect=dashboard.RouteError("refused by policy")
    )
    render = mocker.patch.object(dashboard, "render", wraps=dashboard.render)

    _drive_run(mocker, [b"j", _ENTER, b"g", b"b", _ESC], [group])

    assert _text_prompts(render) == []
    child.assert_not_called()
    assert any("refused by policy" in str(c.kwargs.get("notice")) for c in render.call_args_list)


def test_run_new_base_prompt_offers_the_host_branches(mocker, tmp_path):
    group = dashboard.RepoGroup("alpha", str(tmp_path), None, [_ci("alpha-x", "alpha")])
    mocker.patch.object(dashboard.subprocess, "run")
    mocker.patch.object(dashboard, "new_container_base_default", return_value="main")
    mocker.patch.object(dashboard, "host_branches", side_effect=_fake_branches)
    render = mocker.patch.object(dashboard, "render", wraps=dashboard.render)

    _drive_run(mocker, [b"n", *_keys("feature"), _ENTER], [group])

    base = [p for p in _text_prompts(render) if p.purpose == "new-base"]
    assert base and base[-1].suggestions == ("main", "feat/a", "develop")
    assert base[-1].require_suggestion is False
    assert base[-1].text == "main"


def _sigint_reader(sequence: list[bytes | type[BaseException]]):
    """An ``os.read`` side effect raising the exception classes in ``sequence``,
    and the list of every item it was asked for.

    On a real terminal (cbreak mode, ISIG on) Ctrl-C never reaches ``os.read``
    as a byte: it is SIGINT, i.e. ``KeyboardInterrupt`` out of the blocking
    ``select``/``read`` pair. Trailing reads are a plain ``b"\\x03"`` byte so
    the loop always ends.
    """
    items = iter(sequence)
    reads: list[object] = []

    def read(_fd, _n):
        item = next(items, b"\x03")
        reads.append(item)
        if isinstance(item, type):
            raise item()
        return item

    return read, reads


@pytest.mark.parametrize(
    "before",
    [
        pytest.param([b"n"], id="branch-step"),
        pytest.param([b"n", *_keys("feature"), _ENTER], id="base-step"),
        pytest.param([b"!", *_keys("ls")], id="command-line"),
    ],
)
def test_run_sigint_at_a_text_input_cancels_it_and_keeps_the_dashboard(mocker, tmp_path, before):
    group = dashboard.RepoGroup("alpha", str(tmp_path), None, [_ci("alpha-x", "alpha")])
    child = mocker.patch.object(dashboard.subprocess, "run")
    mocker.patch.object(dashboard, "new_container_base_default", return_value="main")
    render = mocker.patch.object(dashboard, "render", wraps=dashboard.render)

    read, reads = _sigint_reader([*before, KeyboardInterrupt, b"h", _ESC])
    assert _drive_run_with_reader(mocker, read, [group]) == 0

    child.assert_not_called()
    overlays = [c.kwargs.get("overlay") for c in render.call_args_list]
    assert "help" in overlays  # the dashboard outlived the Ctrl-C and took "h"
    assert b"h" in reads


def test_run_sigint_with_no_overlay_still_quits(mocker, tmp_path):
    group = dashboard.RepoGroup("alpha", str(tmp_path), None, [_ci("alpha-x", "alpha")])
    child = mocker.patch.object(dashboard.subprocess, "run")

    read, reads = _sigint_reader([KeyboardInterrupt, b"h"])
    assert _drive_run_with_reader(mocker, read, [group]) == 0

    child.assert_not_called()
    assert reads == [KeyboardInterrupt]  # nothing was read after the interrupt


def test_run_new_from_pr_prompts_for_a_number_and_dispatches(mocker, tmp_path):
    group = dashboard.RepoGroup("alpha", str(tmp_path), None, [_ci("alpha-x", "alpha")])
    child = mocker.patch.object(dashboard.subprocess, "run")
    child.return_value.returncode = 0
    mocker.patch.object(dashboard, "_wait_for_return")

    # repo header → Enter opens the repo menu → Down to "New from PR…" → Enter
    keys = [_ENTER, b"\x1b[B", _ENTER, *_keys("123"), _ENTER]
    assert _drive_run(mocker, keys, [group]) == 0

    child.assert_called_once_with(
        ["jailbee", "new", "--background", "--pr", "123"], check=False, cwd=tmp_path
    )


_NEW_PR_KEYS = [_ENTER, b"\x1b[B", _ENTER, *_keys("123"), _ENTER]


def test_new_container_runs_detached_without_taking_the_terminal(mocker, tmp_path):
    group = dashboard.RepoGroup("alpha", str(tmp_path), None, [_ci("alpha-x", "alpha")])
    child = mocker.patch.object(dashboard.subprocess, "run")
    child.return_value.returncode = 0
    wait = mocker.patch.object(dashboard, "_wait_for_return")

    assert _drive_run(mocker, _NEW_PR_KEYS, [group]) == 0

    child.assert_called_once()
    wait.assert_not_called()  # `foreground` always ends in the "press Enter" stop


def test_new_container_that_wants_an_answer_is_rerun_in_the_foreground(mocker, tmp_path):
    group = dashboard.RepoGroup("alpha", str(tmp_path), None, [_ci("alpha-x", "alpha")])
    detached = mocker.Mock(returncode=2, stderr="error: ... no terminal to ask on. Re-run")
    attended = mocker.Mock(returncode=0, stderr=None)
    child = mocker.patch.object(dashboard.subprocess, "run", side_effect=[detached, attended])
    wait = mocker.patch.object(dashboard, "_wait_for_return")

    assert _drive_run(mocker, _NEW_PR_KEYS, [group]) == 0

    argv = ["jailbee", "new", "--background", "--pr", "123"]
    assert child.call_args_list == [
        mocker.call(argv, check=False, cwd=tmp_path),
        mocker.call(argv, check=False, cwd=tmp_path),
    ]
    wait.assert_called_once()  # the second run is the attended one


def test_new_container_real_failure_is_noticed_not_rerun(mocker, tmp_path):
    group = dashboard.RepoGroup("alpha", str(tmp_path), None, [_ci("alpha-x", "alpha")])
    child = mocker.patch.object(
        dashboard.subprocess,
        "run",
        return_value=mocker.Mock(returncode=1, stderr="warning\nerror: git fetch failed\n"),
    )
    wait = mocker.patch.object(dashboard, "_wait_for_return")
    render = mocker.patch.object(dashboard, "render", wraps=dashboard.render)

    assert _drive_run(mocker, _NEW_PR_KEYS, [group]) == 0

    child.assert_called_once()
    wait.assert_not_called()
    assert any(
        "jailbee new failed: error: git fetch failed" in str(call.kwargs.get("notice", ""))
        for call in render.call_args_list
    )


class _PendingJobs(JobRunner):
    """A runner whose job never finishes, to look at the in-flight state."""

    def start(self, key, label, argv, cwd, on_done):
        if key in self._labels:
            raise ValueError(key)
        self._labels[key] = label


def test_running_job_is_shown_and_not_started_twice(mocker, tmp_path):
    group = dashboard.RepoGroup("alpha", str(tmp_path), None, [_ci("alpha-x", "alpha")])
    mocker.patch.object(dashboard, "new_container_base_default", return_value="main")
    render = mocker.patch.object(dashboard, "render", wraps=dashboard.render)
    typed = [b"n", *_keys("work"), _ENTER, _ENTER, b"n", *_keys("work"), _ENTER, _ENTER]
    mocker.patch.object(dashboard, "_wait_for_return")
    mocker.patch.object(dashboard.subprocess, "run")

    _mock_terminal(mocker)  # installs the synchronous fake; swap in the pending one
    mocker.patch.object(dashboard, "JobRunner", _PendingJobs)
    _fake_state(mocker, [group])
    mocker.patch.object(dashboard.select, "select", return_value=([True], [], []))
    padded = itertools.chain(typed, [b"\x03"], itertools.repeat(b"\x03"))
    mocker.patch.object(dashboard.os, "read", side_effect=lambda fd, n: next(padded))
    dashboard.run(mocker.Mock(), None)

    notices = [str(call.kwargs.get("notice", "")) for call in render.call_args_list]
    assert any("creating work…" in n for n in notices)
    assert any("already being created" in n for n in notices)


def test_run_new_prompt_whose_repo_vanishes_dispatches_nothing(mocker, tmp_path):
    group = dashboard.RepoGroup("alpha", str(tmp_path), None, [_ci("alpha-x", "alpha")])
    live: list[dashboard.RepoGroup] = [group]
    child = mocker.patch.object(dashboard.subprocess, "run")
    mocker.patch.object(dashboard, "new_container_base_default", return_value="main")
    mocker.patch.object(dashboard, "_wait_for_return")
    render = mocker.patch.object(dashboard, "render", wraps=dashboard.render)
    # branch, Enter (repo vanishes here), then Enter to confirm the base
    typed = iter([b"n", *_keys("feature"), _ENTER, _ENTER])

    def read(_fd, _n):
        key = next(typed, b"\x03")
        if key == _ENTER and live:
            live.clear()
        return key

    assert _drive_run_with_reader(mocker, read, live) == 0

    child.assert_not_called()
    # either the loop-top guard or the submit-time lookup explains it
    notices = [str(c.kwargs.get("notice")) for c in render.call_args_list]
    assert any("prompt closed" in n or "no longer listed" in n for n in notices)


def test_run_open_prompt_closes_when_its_repo_vanishes_before_any_submit(mocker, tmp_path):
    """The loop-top guard alone: the repo goes while the user is still typing."""
    group = dashboard.RepoGroup("alpha", str(tmp_path), None, [_ci("alpha-x", "alpha")])
    live: list[dashboard.RepoGroup] = [group]
    child = mocker.patch.object(dashboard.subprocess, "run")
    mocker.patch.object(dashboard, "new_container_base_default", return_value="main")
    render = mocker.patch.object(dashboard, "render", wraps=dashboard.render)
    typed = iter([b"n", b"f", b"x"])  # no Enter: nothing is ever submitted

    def read(_fd, _n):
        key = next(typed, b"\x03")
        if key == b"x":
            live.clear()  # vanishes after "f" was typed, before the next frame
        return key

    assert _drive_run_with_reader(mocker, read, live) == 0

    child.assert_not_called()
    overlays = [c.kwargs.get("overlay") for c in render.call_args_list]
    prompts = [o for o in overlays if isinstance(o, dashboard.TextPrompt)]
    # the last frame with the prompt showed "f"; "x" was typed, then the repo went
    assert prompts[-1].text == "f"
    closed_at = overlays.index(prompts[-1]) + 1
    assert overlays[closed_at] is None
    assert "'alpha' is gone — prompt closed" in str(
        render.call_args_list[closed_at].kwargs.get("notice")
    )


def test_run_empty_repo_header_menu_creates_container(mocker, tmp_path):
    group = dashboard.RepoGroup("empty", str(tmp_path), None, [])
    mocker.patch.object(dashboard, "new_container_base_default", return_value="main")
    mocker.patch.object(dashboard, "_wait_for_return")
    child = mocker.patch.object(dashboard.subprocess, "run")
    child.return_value.returncode = 0

    # Enter opens the repo menu, Enter picks "New container…", then the two answers
    keys = [_ENTER, _ENTER, *_keys("feature"), _ENTER, _ENTER]
    assert _drive_run(mocker, keys, [group]) == 0

    child.assert_called_once_with(
        ["jailbee", "new", "--background", "--", "feature", "main"], check=False, cwd=tmp_path
    )


def test_run_cannot_create_from_a_row_hidden_by_visibility_settings(mocker, tmp_path):
    group = dashboard.RepoGroup("alpha", str(tmp_path), None, [_ci("alpha-one", "alpha")])
    prompt = mocker.patch("typer.prompt")
    child = mocker.patch.object(dashboard.subprocess, "run")
    render = mocker.patch.object(dashboard, "render", wraps=dashboard.render)

    assert (
        _drive_run(mocker, [b"\x1b[B", b"S", b"\t", b"\t", b"\x1b[B", b" ", b"\x1b", b"n"], [group])
        == 0
    )

    prompt.assert_not_called()
    child.assert_not_called()
    assert any("Select a repo" in str(call.kwargs.get("notice")) for call in render.call_args_list)


def test_open_menu_closes_when_its_container_becomes_hidden(mocker, tmp_path):
    group = dashboard.RepoGroup("alpha", str(tmp_path), None, [_ci("alpha-one", "alpha")])
    _mock_terminal(mocker)
    _fake_state(mocker, [group])
    render = mocker.patch.object(dashboard, "render", wraps=dashboard.render)
    child = mocker.patch.object(dashboard.subprocess, "run")
    hiding = False
    filter_groups = dashboard.visible_repo_groups

    def hide_after_menu_opens(groups, *, show_empty_repos, hidden_repos):
        active_hidden = hidden_repos | ({"alpha"} if hiding else set())
        return filter_groups(
            groups, show_empty_repos=show_empty_repos, hidden_repos=frozenset(active_hidden)
        )

    mocker.patch.object(dashboard, "visible_repo_groups", side_effect=hide_after_menu_opens)
    selections = 0

    def advance_to_hidden_snapshot(*args, **kwargs):
        nonlocal hiding, selections
        selections += 1
        if selections == 3:  # menu is open; hide its selected repo before the next frame
            hiding = True
        return ([True], [], [])

    mocker.patch.object(dashboard.select, "select", side_effect=advance_to_hidden_snapshot)
    keys = itertools.chain([b"\x1b[B", b"\r", b"x", b"\r", b"\x03"], itertools.repeat(b"\x03"))
    mocker.patch.object(dashboard.os, "read", side_effect=lambda fd, size: next(keys))

    assert dashboard.run(mocker.Mock(), None) == 0

    overlays = [call.kwargs.get("overlay") for call in render.call_args_list]
    menu_frames = [overlay for overlay in overlays if isinstance(overlay, dashboard.MenuState)]
    assert menu_frames  # the selected container really did have an open action menu
    closed_at = overlays.index(None, overlays.index(menu_frames[-1]) + 1)
    assert not any(isinstance(overlay, dashboard.MenuState) for overlay in overlays[closed_at:])
    assert any("menu closed" in str(call.kwargs.get("notice")) for call in render.call_args_list)
    child.assert_not_called()


def test_settings_key_switches_from_another_overlay_instead_of_closing(mocker):
    """F2/S must mirror ``h``'s own toggle: pressing it while another
    overlay (the action menu, help) is open switches to settings, not just
    closes whatever was open.

    There is no live group in this harness (the fake state client serves
    ``[]``), and Space is only handled by the settings overlay. That makes
    ``save_view_state`` firing after ``h`` then ``S`` then `Space` a
    discriminating signal that ``S`` actually opened the settings overlay,
    whose Space handling calls ``persist_view_state``, rather than merely
    closing help and leaving the bare table to reject the keypress silently.
    Fails if `"settings"` goes back to being grouped
    with `("cancel", "quit")`, which only closes whatever overlay is open.
    """
    save = mocker.patch.object(dashboard, "save_view_state")
    rc = _drive_run(mocker, [b"h", b"S", b" "])

    assert rc == 0
    save.assert_called_once()


def test_run_dispatches_n_to_start_new_container(mocker):
    """Drive the `n` key through `run()`'s real dispatch (``elif key ==
    "new": overlay = start_new_container()``), not just `parse_key`/the binding shape in
    isolation — a typo in that `elif` arm would be caught by nothing else.

    ``_drive_run``'s fake state client serves no containers, so nothing is
    selected and ``start_new_container`` takes its notice path (`new_container_
    reject_note` returning "Select a repo or a container first") without
    prompting or spawning anything. `render` is wrapped rather than replaced
    so `Live` still gets a real renderable; its calls are inspected for the
    notice text that would otherwise only ever be visible on a real screen.
    """
    render = mocker.patch.object(dashboard, "render", wraps=dashboard.render)

    rc = _drive_run(mocker, [b"n"])

    assert rc == 0
    notices = [call.kwargs.get("notice") for call in render.call_args_list]
    assert "Select a repo or a container first" in notices


def test_repo_header_enter_opens_menu_without_folding(mocker, tmp_path):
    group = dashboard.RepoGroup("alpha", str(tmp_path), None, [_ci("alpha-x", "alpha")])
    render = mocker.patch.object(dashboard, "render", wraps=dashboard.render)
    save = mocker.patch.object(dashboard, "save_view_state")

    assert _drive_run(mocker, [b"\r"], groups=[group]) == 0

    menus = [call.kwargs["overlay"] for call in render.call_args_list if call.kwargs["overlay"]]
    assert menus
    assert menus[0].repo == "alpha"
    assert [
        item.label if isinstance(item, dashboard.MenuGroup) else item[0]
        for item in menus[0].actions
    ] == [
        "New container…",
        "New from PR…",
        "Credential group…",
        "Accounts…",
        "Network →",
        "Apply config…",
        "Diagnostics →",
        "Prune stale containers…",
        "Fold",
    ]
    save.assert_not_called()


def test_repo_menu_new_runs_the_existing_creation_flow(mocker, tmp_path):
    group = dashboard.RepoGroup("alpha", str(tmp_path), None, [_ci("alpha-x", "alpha")])
    mocker.patch.object(dashboard, "new_container_base_default", return_value="develop")
    mocker.patch.object(dashboard, "_wait_for_return")
    child = mocker.patch.object(dashboard.subprocess, "run")
    child.return_value.returncode = 0

    # the base field is prefilled with "develop": erase it and type another base
    keys = [_ENTER, _ENTER, *_keys("feature"), _ENTER, *[b"\x7f"] * 7, *_keys("main"), _ENTER]
    assert _drive_run(mocker, keys, groups=[group]) == 0

    child.assert_called_once_with(
        ["jailbee", "new", "--background", "--", "feature", "main"], check=False, cwd=tmp_path
    )


def test_repo_menu_new_from_pr_runs_review_creation_in_repo(mocker, tmp_path):
    group = dashboard.RepoGroup("alpha", str(tmp_path), None, [])
    mocker.patch.object(dashboard, "_wait_for_return")
    child = mocker.patch.object(dashboard.subprocess, "run")
    child.return_value.returncode = 0

    keys = [_ENTER, b"j", _ENTER, *_keys("123"), _ENTER]
    assert _drive_run(mocker, keys, groups=[group]) == 0

    child.assert_called_once_with(
        ["jailbee", "new", "--background", "--pr", "123"], check=False, cwd=tmp_path
    )


_NOT_A_PR = "PR number must be a positive whole number"


@pytest.mark.parametrize(
    ("answer", "error"),
    [
        ("0", _NOT_A_PR),
        ("-2", _NOT_A_PR),
        ("abc", _NOT_A_PR),
        ("--yes", _NOT_A_PR),
        ("  ", "PR number cannot be empty"),
        pytest.param("9" * 5000, _NOT_A_PR, id="oversized"),
    ],
)
def test_repo_menu_new_from_pr_rejects_nonpositive_or_non_numeric_input(
    mocker, tmp_path, answer, error
):
    group = dashboard.RepoGroup("alpha", str(tmp_path), None, [])
    mocker.patch.object(dashboard, "_wait_for_return")
    child = mocker.patch.object(dashboard.subprocess, "run")
    render = mocker.patch.object(dashboard, "render", wraps=dashboard.render)

    # the answer arrives as one read (a paste), so the oversized case stays one frame
    keys = [_ENTER, b"j", _ENTER, answer.encode(), _ENTER]
    assert _drive_run(mocker, keys, groups=[group]) == 0

    child.assert_not_called()
    prompts = [
        c.kwargs["overlay"]
        for c in render.call_args_list
        if isinstance(c.kwargs.get("overlay"), dashboard.TextPrompt)
    ]
    assert prompts[-1].purpose == "new-pr"
    assert prompts[-1].text == answer
    assert prompts[-1].error == error


@pytest.mark.parametrize("initially_folded", [False, True])
def test_repo_menu_toggles_fold_and_persists_it(mocker, tmp_path, initially_folded):
    group = dashboard.RepoGroup("alpha", str(tmp_path), None, [_ci("alpha-x", "alpha")])
    from jailbee.db.view_prefs import FRONTEND_TUI

    save = mocker.patch.object(dashboard, "save_view_state")
    render = mocker.patch.object(dashboard, "render", wraps=dashboard.render)

    assert (
        _drive_run(
            mocker,
            _repo_menu_keys(group, "fold"),
            groups=[group],
            view_state=dashboard.ViewState(
                folded=frozenset({"alpha"}) if initially_folded else frozenset(),
                show_empty_repos=False,
                hidden_repos=frozenset({"other"}),
            ),
        )
        == 0
    )

    menus = [call.kwargs["overlay"] for call in render.call_args_list if call.kwargs["overlay"]]
    assert menus[0].actions[-1] == (("Unfold" if initially_folded else "Fold"), "fold")
    assert save.call_count == 1
    assert save.call_args.args[1] == FRONTEND_TUI
    assert save.call_args.args[2].folded == (
        frozenset() if initially_folded else frozenset({"alpha"})
    )
    assert save.call_args.args[2].show_empty_repos is False
    assert save.call_args.args[2].hidden_repos == frozenset({"other"})


def test_orphan_repo_menu_only_offers_folding(mocker):
    group = dashboard.RepoGroup("orphan", None, None, [_ci("orphan-x", "orphan")])
    render = mocker.patch.object(dashboard, "render", wraps=dashboard.render)
    child = mocker.patch.object(dashboard.subprocess, "run")
    mocker.patch.object(dashboard, "save_view_state")

    assert _drive_run(mocker, [b"\r", b"\r"], groups=[group]) == 0

    menus = [call.kwargs["overlay"] for call in render.call_args_list if call.kwargs["overlay"]]
    assert menus[0].actions == [("Fold", "fold")]
    child.assert_not_called()


# --- Repo-level CLI entries (apply, diagnostics, prune) ----------------------


def _cfg_group(tmp_path: Path, containers: tuple[ContainerInfo, ...] = ()) -> dashboard.RepoGroup:
    """Repo ``alpha`` with a config path: a local child gets ``--config``, an SSH one must not."""
    return dashboard.RepoGroup(
        "alpha", str(tmp_path), tmp_path / ".jailbee" / "config.yaml", list(containers)
    )


def _repo_menu_keys(group: dashboard.RepoGroup, verb: str, **menu_kwargs) -> list[bytes]:
    """Keys that choose repo-menu ``verb`` from the first row (the repo header).

    Finds a top-level leaf or one inside a submenu, so no test counts entries.
    ``menu_kwargs`` (``ssh_policy``/``over_ssh``) must match the ``run()`` call.
    """
    menu = dashboard.open_repo_menu([group], group.prefix, frozenset(), **menu_kwargs)
    assert menu is not None
    for i, item in enumerate(menu.actions):
        if isinstance(item, dashboard.MenuGroup):
            leaves = [leaf_verb for _label, leaf_verb in item.actions]
            if verb in leaves:
                return [_ENTER, *[b"j"] * i, _ENTER, *[b"j"] * leaves.index(verb), _ENTER]
        elif item[1] == verb:
            return [_ENTER, *[b"j"] * i, _ENTER]
    raise AssertionError(f"{verb!r} is not in the repo menu")


def _repo_menu_verbs(menu: dashboard.RepoMenuState | None) -> set[str]:
    """Every leaf verb of a repo menu, submenus included."""
    assert menu is not None
    return {
        leaf[1]
        for item in menu.actions
        for leaf in (item.actions if isinstance(item, dashboard.MenuGroup) else (item,))
    }


def _notices(render) -> list[str]:
    return [c.kwargs["notice"] for c in render.call_args_list if c.kwargs.get("notice")]


def test_repo_menu_offers_apply_after_network_and_before_fold(tmp_path):
    menu = dashboard.open_repo_menu([_cfg_group(tmp_path)], "alpha", frozenset())
    assert menu is not None
    labels = [i.label if isinstance(i, dashboard.MenuGroup) else i[0] for i in menu.actions]
    assert labels.index("Network →") < labels.index("Apply config…") < labels.index("Fold")


def test_orphan_repo_menu_offers_no_apply():
    group = dashboard.RepoGroup("orphan", None, None, [_ci("orphan-x", "orphan")])
    assert "apply" not in _repo_menu_verbs(dashboard.open_repo_menu([group], "orphan", frozenset()))


@pytest.mark.parametrize(
    ("over_ssh", "policy_kwargs", "offered"),
    [
        (False, None, True),
        (False, {"commands": {"mode": "disabled"}}, True),
        (True, {}, False),
        (True, {"commands": {"mode": "allowlist", "allow": ["apply"]}}, False),
        (
            True,
            {"commands": {"mode": "allowlist", "allow": ["apply"]}, "restrict_host": False},
            True,
        ),
        (
            True,
            {"commands": {"mode": "allowlist", "allow": ["shell"]}, "restrict_host": False},
            False,
        ),
    ],
    ids=[
        "local",
        "local-ignores-policy",
        "ssh-default",
        "ssh-allowlist-restricted",
        "ssh-allowlist-unrestricted",
        "ssh-allowlist-without-it",
    ],
)
def test_repo_menu_apply_follows_the_ssh_policy(tmp_path, over_ssh, policy_kwargs, offered):
    menu = dashboard.open_repo_menu(
        [_cfg_group(tmp_path)],
        "alpha",
        frozenset(),
        ssh_policy=_ssh_policy(policy_kwargs),
        over_ssh=over_ssh,
    )
    assert ("apply" in _repo_menu_verbs(menu)) is offered


@pytest.mark.parametrize(
    ("downs", "tail"), [(0, []), (1, ["--no-restart"])], ids=["restart", "no-restart"]
)
def test_repo_apply_runs_in_the_terminal_with_the_chosen_restart_policy(
    mocker, tmp_path, downs, tail
):
    group = _cfg_group(tmp_path)
    child = mocker.patch.object(dashboard.subprocess, "run")
    child.return_value.returncode = 0
    wait = mocker.patch.object(dashboard, "_wait_for_return")
    render = mocker.patch.object(dashboard, "render", wraps=dashboard.render)

    keys = [*_repo_menu_keys(group, "apply"), *[b"j"] * downs, _ENTER]
    assert _drive_run(mocker, keys, [group]) == 0

    picker = _rendered(render, dashboard.Picker)[0]
    assert [e.value for e in picker.entries] == ["restart", "no-restart"]
    child.assert_called_once_with(
        ["jailbee", "apply", *tail, "--config", str(group.config_path)], check=False, cwd=tmp_path
    )
    wait.assert_called_once()


@pytest.mark.parametrize("key", [_ESC, b"\x03"], ids=["escape", "ctrl-c"])
def test_repo_apply_cancel_runs_nothing(mocker, tmp_path, key):
    group = _cfg_group(tmp_path)
    child = mocker.patch.object(dashboard.subprocess, "run")
    render = mocker.patch.object(dashboard, "render", wraps=dashboard.render)

    assert _drive_run(mocker, [*_repo_menu_keys(group, "apply"), key], [group]) == 0

    child.assert_not_called()
    assert _rendered(render, dashboard.Picker)
    if key == _ESC:
        assert "Cancelled" in _notices(render)


def test_repo_apply_over_unrestricted_ssh_sends_no_config_flag(mocker, tmp_path):
    from jailbee.config.models_remote import RemoteSSHConfig

    group = _cfg_group(tmp_path)
    policy = RemoteSSHConfig(restrict_host=False)
    child = mocker.patch.object(dashboard.subprocess, "run")
    child.return_value.returncode = 0
    mocker.patch.object(dashboard, "_wait_for_return")

    keys = [*_repo_menu_keys(group, "apply", ssh_policy=policy, over_ssh=True), _ENTER]
    assert _drive_run(mocker, keys, [group], over_ssh=True, ssh_policy=policy) == 0

    child.assert_called_once_with(["jailbee", "apply"], check=False, cwd=tmp_path)


def test_repo_apply_is_not_offered_to_a_default_ssh_session(mocker, tmp_path):
    from jailbee.config.models_remote import RemoteSSHConfig

    group = _cfg_group(tmp_path)
    render = mocker.patch.object(dashboard, "render", wraps=dashboard.render)

    assert (
        _drive_run(
            mocker, [_ENTER], [group], remote=True, over_ssh=True, ssh_policy=RemoteSSHConfig()
        )
        == 0
    )

    menus = _rendered(render, dashboard.RepoMenuState)
    assert menus and "apply" not in _repo_menu_verbs(menus[0])


def test_repo_apply_nonzero_exit_is_a_notice(mocker, tmp_path):
    group = _cfg_group(tmp_path)
    child = mocker.patch.object(dashboard.subprocess, "run")
    child.return_value.returncode = 2
    mocker.patch.object(dashboard, "_wait_for_return")
    render = mocker.patch.object(dashboard, "render", wraps=dashboard.render)

    _drive_run(mocker, [*_repo_menu_keys(group, "apply"), _ENTER], [group])

    assert "'jailbee apply' exited 2" in _notices(render)


# --- Credential group… in the repo menu ------------------------------------

_TEAM_ROWS = (
    '[{"agent": "claude", "group": "team", "account": null, "state": "empty",'
    ' "repos": [], "containers": []}]'
)
# repo header → Enter (menu) → past New container…, New from PR… → Enter
_OPEN_REPO_GROUP_PICKER = [_ENTER, b"j", b"j", _ENTER]


def _fake_account_cli(mocker, *, listing, change=None):
    """Patch the quiet CLI runner: the group listing answers ``listing``, a change ``change``."""
    change = change or dashboard.da.CliResult(True, "Set.")

    def fake(argv, **_kwargs):
        return listing if argv[:3] == ["account", "group", "ls"] else change

    return mocker.patch.object(dashboard.da, "run_cli_quiet", side_effect=fake)


def _groups_listing(stdout: str) -> dashboard.da.CliResult:
    return dashboard.da.CliResult(True, "done", stdout)


def _rendered(render, kind):
    """Every overlay of type ``kind`` a frame drew, in order."""
    overlays = (c.kwargs["overlay"] for c in render.call_args_list)
    return [overlay for overlay in overlays if isinstance(overlay, kind)]


def test_repo_menu_offers_credential_group_after_the_creation_entries():
    group = dashboard.RepoGroup("alpha", "/alpha", None, [])
    menu = dashboard.open_repo_menu([group], "alpha", frozenset())
    assert menu is not None
    assert menu.actions[2] == ("Credential group…", "credential-group")


# (over_ssh, RemoteSSHConfig kwargs, is Credential group… offered)
_CREDENTIAL_GROUP_POLICY_CASES = pytest.mark.parametrize(
    ("over_ssh", "policy_kwargs", "offered"),
    [
        (False, None, True),
        # a local dashboard never consults the SSH policy, however strict
        (False, {"commands": {"mode": "disabled"}}, True),
        (True, {"commands": {"mode": "allowlist", "allow": ["new", "tmux"]}}, False),
        # full, but `account` writes are host commands under restrict_host
        (True, {}, False),
        (True, {"restrict_host": False}, True),
    ],
    ids=[
        "local",
        "local-ignores-policy",
        "ssh-allowlist-without-it",
        "ssh-default-restrict-host",
        "ssh-unrestricted",
    ],
)


def _ssh_policy(policy_kwargs):
    from jailbee.config.models_remote import RemoteSSHConfig

    return None if policy_kwargs is None else RemoteSSHConfig.model_validate(policy_kwargs)


@_CREDENTIAL_GROUP_POLICY_CASES
def test_repo_menu_credential_group_follows_the_ssh_policy(over_ssh, policy_kwargs, offered):
    group = dashboard.RepoGroup("alpha", "/alpha", None, [])
    menu = dashboard.open_repo_menu(
        [group], "alpha", frozenset(), ssh_policy=_ssh_policy(policy_kwargs), over_ssh=over_ssh
    )
    assert menu is not None
    verbs = [item[1] for item in menu.actions if not isinstance(item, dashboard.MenuGroup)]
    assert ("credential-group" in verbs) is offered


def test_repo_credential_group_flow_sets_the_chosen_group(mocker, tmp_path):
    group = dashboard.RepoGroup("alpha", str(tmp_path), None, [_ci("alpha-x", "alpha")])
    run = _fake_account_cli(mocker, listing=_groups_listing(_TEAM_ROWS))
    child = mocker.patch.object(dashboard.subprocess, "run")
    render = mocker.patch.object(dashboard, "render", wraps=dashboard.render)

    # the picker opens on its first entry, the only group: "team"
    assert _drive_run(mocker, [*_OPEN_REPO_GROUP_PICKER, _ENTER], [group]) == 0

    assert run.call_args_list == [
        mocker.call(dashboard.da.group_ls_argv(), cwd=tmp_path),
        mocker.call(["account", "group", "set", "team"], cwd=tmp_path),
    ]
    child.assert_not_called()  # quiet: the terminal was never handed over
    calls = render.call_args_list
    shown = [i for i, c in enumerate(calls) if isinstance(c.kwargs["overlay"], dashboard.Picker)]
    assert shown, "the group picker was never drawn"
    picker = calls[shown[0]].kwargs["overlay"]
    assert (picker.purpose, picker.target, picker.title) == (
        "repo-group",
        "alpha",
        "Credential group — alpha",
    )
    assert calls[shown[0]].args[1] == dashboard.Row("repo", "alpha")
    assert calls[-1].kwargs["overlay"] is None
    assert calls[-1].kwargs["notice"] == "Set."


@pytest.mark.parametrize("cancel", [_ESC, b"\x03"], ids=["esc", "ctrl-c"])
def test_credential_group_picker_offers_none_and_host_default_even_with_no_groups(
    mocker, tmp_path, cancel
):
    group = dashboard.RepoGroup("alpha", str(tmp_path), None, [])
    run = _fake_account_cli(mocker, listing=_groups_listing("[]"))
    render = mocker.patch.object(dashboard, "render", wraps=dashboard.render)

    assert _drive_run(mocker, [*_OPEN_REPO_GROUP_PICKER, cancel], [group]) == 0

    pickers = _rendered(render, dashboard.Picker)
    assert [e.label for e in pickers[0].entries] == [
        "none (this repo keeps its own login)",
        "Use the host default",
        "New group…",
    ]
    assert run.call_count == 1  # the listing; the cancel ran nothing
    # a frame after the cancel: the picker closed, the dashboard did not
    assert render.call_args_list[-1].kwargs["overlay"] is None


def _sigint_or(script):
    """An ``os.read`` fake: ``"SIGINT"`` raises like a real Ctrl-C under cbreak."""

    def read(_fd, _n):
        item = next(script, b"\x03")
        if item == "SIGINT":
            raise KeyboardInterrupt
        return item

    return read


@pytest.mark.parametrize("ctrl_c", [b"\x03", "SIGINT"], ids=["byte", "keyboard-interrupt"])
def test_ctrl_c_at_the_group_picker_cancels_it_and_the_dashboard_keeps_running(
    mocker, tmp_path, ctrl_c
):
    group = dashboard.RepoGroup("alpha", str(tmp_path), None, [])
    run = _fake_account_cli(mocker, listing=_groups_listing("[]"))
    render = mocker.patch.object(dashboard, "render", wraps=dashboard.render)
    # cancel the picker, then Enter on the repo header must open its menu again
    script = iter([*_OPEN_REPO_GROUP_PICKER, ctrl_c, _ENTER])

    assert _drive_run_with_reader(mocker, _sigint_or(script), [group]) == 0

    calls = render.call_args_list
    picker_at = max(
        i for i, c in enumerate(calls) if isinstance(c.kwargs["overlay"], dashboard.Picker)
    )
    cancelled = calls[picker_at + 1].kwargs
    assert cancelled["overlay"] is None
    assert cancelled["notice"] == "Cancelled"
    assert isinstance(calls[picker_at + 2].kwargs["overlay"], dashboard.RepoMenuState)
    assert run.call_count == 1


@pytest.mark.parametrize("ctrl_c", [b"\x03", "SIGINT"], ids=["byte", "keyboard-interrupt"])
def test_ctrl_c_without_an_overlay_still_quits(mocker, tmp_path, ctrl_c):
    group = dashboard.RepoGroup("alpha", str(tmp_path), None, [])
    reads = []
    script = iter([ctrl_c])

    def read(fd, n):
        reads.append(n)
        # EOF after the script: a regression quits with a wrong read count
        # instead of hanging the suite on an endless stream of keys.
        return _sigint_or(script)(fd, n) if len(reads) == 1 else b""

    assert _drive_run_with_reader(mocker, read, [group]) == 0
    assert len(reads) == 1  # the first Ctrl-C ended the loop


def test_eof_at_the_group_picker_still_quits(mocker, tmp_path):
    """A closed stdin reads b"" forever; cancelling on it would spin the loop."""
    group = dashboard.RepoGroup("alpha", str(tmp_path), None, [])
    _fake_account_cli(mocker, listing=_groups_listing("[]"))
    render = mocker.patch.object(dashboard, "render", wraps=dashboard.render)
    reads = []
    script = iter(_OPEN_REPO_GROUP_PICKER)

    def read(_fd, _n):
        reads.append(1)
        return next(script, b"")

    assert _drive_run_with_reader(mocker, read, [group]) == 0
    assert len(reads) == len(_OPEN_REPO_GROUP_PICKER) + 1
    assert isinstance(render.call_args_list[-1].kwargs["overlay"], dashboard.Picker)


def test_credential_group_picker_lists_each_group_once_in_order(mocker, tmp_path):
    rows = (
        '[{"agent": "claude", "group": "team", "account": "a", "state": "live",'
        ' "repos": [], "containers": []},'
        ' {"agent": "codex", "group": "team", "account": null, "state": "empty",'
        ' "repos": [], "containers": []},'
        ' {"agent": "claude", "group": "solo", "account": null, "state": "empty",'
        ' "repos": [], "containers": []},'
        ' {"agent": "claude", "group": null, "account": "b", "state": "parked",'
        ' "repos": [], "containers": []}]'
    )
    group = dashboard.RepoGroup("alpha", str(tmp_path), None, [])
    _fake_account_cli(mocker, listing=_groups_listing(rows))
    render = mocker.patch.object(dashboard, "render", wraps=dashboard.render)

    assert _drive_run(mocker, [*_OPEN_REPO_GROUP_PICKER, _ESC], [group]) == 0

    entries = _rendered(render, dashboard.Picker)[0].entries
    assert [(e.label, e.value) for e in entries[:2]] == [("solo", "solo"), ("team", "team")]
    assert len(entries) == 5


def test_credential_group_picker_hides_a_legacy_group_named_none(mocker, tmp_path):
    """`none` spells "no group"; a legacy group of that name must not be offered twice."""
    rows = (
        '[{"agent": "claude", "group": "none", "account": null, "state": "empty",'
        ' "repos": [], "containers": []},'
        ' {"agent": "claude", "group": "team", "account": null, "state": "empty",'
        ' "repos": [], "containers": []}]'
    )
    group = dashboard.RepoGroup("alpha", str(tmp_path), None, [])
    _fake_account_cli(mocker, listing=_groups_listing(rows))
    render = mocker.patch.object(dashboard, "render", wraps=dashboard.render)

    assert _drive_run(mocker, [*_OPEN_REPO_GROUP_PICKER, _ESC], [group]) == 0

    entries = _rendered(render, dashboard.Picker)[0].entries
    assert [(e.label, e.value) for e in entries] == [
        ("team", "team"),
        ("none (this repo keeps its own login)", "none"),
        ("Use the host default", "__unset__"),
        ("New group…", "__new__"),
    ]


@pytest.mark.parametrize(
    ("downs", "argv"),
    [
        (0, ["account", "group", "set", "none"]),
        (1, ["account", "group", "unset"]),
    ],
    ids=["none", "host-default"],
)
def test_credential_group_picker_non_group_choices_run_their_command(mocker, tmp_path, downs, argv):
    group = dashboard.RepoGroup("alpha", str(tmp_path), None, [])
    run = _fake_account_cli(mocker, listing=_groups_listing("[]"))

    keys = [*_OPEN_REPO_GROUP_PICKER, *[b"j"] * downs, _ENTER]
    assert _drive_run(mocker, keys, [group]) == 0

    assert run.call_args_list[-1] == mocker.call(argv, cwd=tmp_path)
    assert run.call_count == 2


@pytest.mark.parametrize(
    ("listing", "reason"),
    [
        (dashboard.da.CliResult(False, "error: boom"), "boom"),
        (dashboard.da.CliResult(True, "done", "not json"), "unexpected output"),
    ],
    ids=["command-failed", "garbled-output"],
)
def test_credential_group_listing_failure_is_a_notice_not_a_traceback(
    mocker, tmp_path, listing, reason
):
    group = dashboard.RepoGroup("alpha", str(tmp_path), None, [])
    run = _fake_account_cli(mocker, listing=listing)
    render = mocker.patch.object(dashboard, "render", wraps=dashboard.render)

    assert _drive_run(mocker, _OPEN_REPO_GROUP_PICKER, [group]) == 0

    assert run.call_count == 1
    assert not _rendered(render, dashboard.Picker)
    last = render.call_args_list[-1].kwargs
    assert last["overlay"] is None
    assert "could not list credential groups" in last["notice"]
    assert reason in last["notice"]


def test_new_group_name_prompt_esc_runs_nothing(mocker, tmp_path):
    group = dashboard.RepoGroup("alpha", str(tmp_path), None, [])
    run = _fake_account_cli(mocker, listing=_groups_listing(_TEAM_ROWS))
    render = mocker.patch.object(dashboard, "render", wraps=dashboard.render)

    # team, none, host default, New group…
    keys = [*_OPEN_REPO_GROUP_PICKER, *[b"j"] * 3, _ENTER, *_keys("fresh"), _ESC]
    assert _drive_run(mocker, keys, [group]) == 0

    prompts = _rendered(render, dashboard.TextPrompt)
    assert prompts and prompts[0].purpose == "repo-group-name"
    assert prompts[-1].text == "fresh"
    assert run.call_count == 1  # only the listing
    last = render.call_args_list[-1].kwargs
    assert last["overlay"] is None
    assert last["notice"] == "Cancelled"


def test_new_group_name_prompt_sets_the_typed_group(mocker, tmp_path):
    group = dashboard.RepoGroup("alpha", str(tmp_path), None, [])
    run = _fake_account_cli(mocker, listing=_groups_listing("[]"))

    keys = [*_OPEN_REPO_GROUP_PICKER, *[b"j"] * 2, _ENTER, *_keys("fresh"), _ENTER]
    assert _drive_run(mocker, keys, [group]) == 0

    assert run.call_args_list[-1] == mocker.call(["account", "group", "set", "fresh"], cwd=tmp_path)


@pytest.mark.parametrize(
    ("change", "stays_up"),
    [
        (dashboard.da.CliResult(False, "an agent is running; pass --force"), True),
        (dashboard.da.CliResult(True, "This repo now uses group `team`."), False),
    ],
    ids=["failure", "success"],
)
def test_account_command_failure_shows_a_long_notice(mocker, tmp_path, change, stays_up):
    """A refusal outlives the ordinary 2.5 s notice; a success does not."""
    group = dashboard.RepoGroup("alpha", str(tmp_path), None, [])
    _fake_account_cli(mocker, listing=_groups_listing(_TEAM_ROWS), change=change)
    render = mocker.patch.object(dashboard, "render", wraps=dashboard.render)
    clock = [1000.0]
    mocker.patch.object(dashboard.time, "monotonic", side_effect=lambda: clock[0])
    assert dashboard.parse_key(b"z") == ""  # an unbound key: one more frame, nothing else
    script = iter([*_OPEN_REPO_GROUP_PICKER, _ENTER, "advance", b"z"])

    def read(_fd, _n):
        item = next(script, b"\x03")
        if item == "advance":
            clock[0] += 5.0
            return b"z"
        return item

    _mock_terminal(mocker)
    _fake_state(mocker, [group])
    mocker.patch.object(dashboard.select, "select", return_value=([True], [], []))
    mocker.patch.object(dashboard.os, "read", side_effect=read)

    assert dashboard.run(mocker.Mock(), None) == 0

    notices = [c.kwargs["notice"] for c in render.call_args_list]
    assert change.message in notices  # shown right after the command
    # the frame drawn 5 s later, and the dashboard still running to draw it
    assert (notices[-1] == change.message) is stays_up


@pytest.mark.parametrize("over_ssh", [False, True], ids=["local", "over-ssh"])
def test_repo_credential_group_config_flag_is_local_only(mocker, tmp_path, over_ssh):
    from jailbee.config.models_remote import RemoteSSHConfig

    config_path = tmp_path / ".jailbee" / "config.yaml"
    group = dashboard.RepoGroup("alpha", str(tmp_path), config_path, [])
    run = _fake_account_cli(mocker, listing=_groups_listing(_TEAM_ROWS))

    assert (
        _drive_run(
            mocker,
            [*_OPEN_REPO_GROUP_PICKER, _ENTER],
            [group],
            remote=over_ssh,
            over_ssh=over_ssh,
            ssh_policy=RemoteSSHConfig(restrict_host=False) if over_ssh else None,
        )
        == 0
    )

    flags = [] if over_ssh else ["--config", str(config_path)]
    assert [c.args[0] for c in run.call_args_list] == [
        [*dashboard.da.group_ls_argv(), *flags],
        ["account", "group", "set", "team", *flags],
    ]


# --- Credential group… in the container menu --------------------------------

_CREDENTIAL_GROUP_LEAF = ("Credential group…", "credential-group")


def _open_container_group_picker(group: dashboard.RepoGroup, **menu_kwargs) -> list[bytes]:
    """Keys that open the first container's credential-group picker.

    ``menu_kwargs`` must match the ``run()`` call, as in ``_container_egress_keys``.
    """
    menu = dashboard.open_menu([group], group.containers[0].name, **menu_kwargs)
    assert menu is not None
    at = list(dashboard._menu_entries(menu)).index(_CREDENTIAL_GROUP_LEAF)
    return [b"j", _ENTER, *[b"j"] * at, _ENTER]


def test_container_menu_offers_credential_group_just_before_network_and_lifecycle(tmp_path):
    # The offered leaves keep it before network; the terminal draws it after
    # the Git/PR/Lifecycle/Network block.
    group = dashboard.RepoGroup("alpha", str(tmp_path), None, [_ci("alpha-x", "alpha")])
    menu = dashboard.open_menu([group], "alpha-x")
    assert menu is not None
    verbs = [verb for _label, verb in menu.actions]
    at = verbs.index("credential-group")
    assert verbs[at + 1].startswith("net ")
    assert not any(v.startswith("net ") for v in verbs[:at])
    assert at < verbs.index("restart")
    # in the drawn menu it sits right above the Network → group
    entries = list(dashboard._menu_entries(menu))
    labels = [i.label if isinstance(i, dashboard.MenuGroup) else i[0] for i in entries]
    assert labels.index("Network →") < entries.index(_CREDENTIAL_GROUP_LEAF)


def test_stopped_container_menu_offers_credential_group_before_egress(tmp_path):
    group = dashboard.RepoGroup("alpha", str(tmp_path), None, [_ci("alpha-x", "alpha", "Stopped")])
    menu = dashboard.open_menu([group], "alpha-x")
    assert menu is not None
    verbs = [verb for _label, verb in menu.actions]
    assert verbs[verbs.index("credential-group") + 1] == "net egress ls"


@pytest.mark.parametrize(
    ("verbs", "expected"),
    [
        (["tmux", "restart", "stop", "destroy"], ["tmux", "credential-group", "restart"]),
        (["destroy"], ["credential-group", "destroy"]),
        (["tmux"], ["tmux", "credential-group"]),
    ],
    ids=["lifecycle-without-network", "destroy-only", "neither"],
)
def test_credential_group_falls_back_to_before_lifecycle_or_last(verbs, expected):
    actions = [(verb.title(), verb) for verb in verbs]
    placed = [verb for _label, verb in dashboard._with_credential_group(actions)]
    assert placed[: len(expected)] == expected
    assert placed.count("credential-group") == 1


def test_shared_action_list_stays_free_of_the_terminal_only_entry(tmp_path):
    """The Qt dashboard reads `actions_for_container`; it has no handler for the verb."""
    group = dashboard.RepoGroup("alpha", str(tmp_path), None, [_ci("alpha-x", "alpha")])
    assert "credential-group" not in {
        v for _l, v in dashboard.actions_for_container([group], "alpha-x")
    }


@_CREDENTIAL_GROUP_POLICY_CASES
def test_container_menu_credential_group_follows_the_ssh_policy(
    tmp_path, over_ssh, policy_kwargs, offered
):
    group = dashboard.RepoGroup("alpha", str(tmp_path), None, [_ci("alpha-x", "alpha")])
    menu = dashboard.open_menu(
        [group],
        "alpha-x",
        remote=over_ssh,
        over_ssh=over_ssh,
        ssh_policy=_ssh_policy(policy_kwargs),
    )
    assert menu is not None
    assert (_CREDENTIAL_GROUP_LEAF in menu.actions) is offered


@pytest.mark.parametrize(
    ("downs", "argv"),
    [
        (0, ["account", "group", "use", "team", "alpha-x"]),
        (1, ["account", "group", "use", "none", "alpha-x"]),
        (2, ["account", "group", "reset", "alpha-x"]),
    ],
    ids=["team", "none", "follow-the-repo"],
)
def test_container_credential_group_flow_uses_the_container_and_reset(
    mocker, tmp_path, downs, argv
):
    group = dashboard.RepoGroup("alpha", str(tmp_path), None, [_ci("alpha-x", "alpha")])
    run = _fake_account_cli(mocker, listing=_groups_listing(_TEAM_ROWS))
    child = mocker.patch.object(dashboard.subprocess, "run")
    render = mocker.patch.object(dashboard, "render", wraps=dashboard.render)

    keys = [*_open_container_group_picker(group), *[b"j"] * downs, _ENTER]
    assert _drive_run(mocker, keys, [group]) == 0

    assert run.call_args_list == [
        mocker.call(dashboard.da.group_ls_argv(), cwd=tmp_path),
        mocker.call(argv, cwd=tmp_path),
    ]
    child.assert_not_called()  # never dispatched to the CLI as a menu verb
    calls = render.call_args_list
    shown = [i for i, c in enumerate(calls) if isinstance(c.kwargs["overlay"], dashboard.Picker)]
    picker = calls[shown[0]].kwargs["overlay"]
    assert (picker.purpose, picker.target) == ("container-group", "alpha-x")
    assert [e.label for e in picker.entries] == [
        "team",
        "none (this container keeps its own login)",
        "Follow the repo's group",
        "New group…",
    ]
    # the cursor stays on the container while the picker is open
    assert {calls[i].args[1] for i in shown} == {dashboard.Row("container", "alpha-x")}
    assert calls[-1].kwargs["overlay"] is None


@pytest.mark.parametrize("over_ssh", [False, True], ids=["local", "over-ssh"])
def test_container_credential_group_config_flag_is_local_only(mocker, tmp_path, over_ssh):
    from jailbee.config.models_remote import RemoteSSHConfig

    config_path = tmp_path / ".jailbee" / "config.yaml"
    group = dashboard.RepoGroup("alpha", str(tmp_path), config_path, [_ci("alpha-x", "alpha")])
    run = _fake_account_cli(mocker, listing=_groups_listing(_TEAM_ROWS))
    policy = RemoteSSHConfig(restrict_host=False) if over_ssh else None
    menu_kwargs = {"remote": over_ssh, "over_ssh": over_ssh, "ssh_policy": policy}

    keys = [*_open_container_group_picker(group, **menu_kwargs), _ENTER]
    assert _drive_run(mocker, keys, [group], **menu_kwargs) == 0

    flags = [] if over_ssh else ["--config", str(config_path)]
    assert [c.args[0] for c in run.call_args_list] == [
        [*dashboard.da.group_ls_argv(), *flags],
        ["account", "group", "use", "team", "alpha-x", *flags],
    ]


def test_container_new_group_name_prompt_uses_the_typed_group(mocker, tmp_path):
    group = dashboard.RepoGroup("alpha", str(tmp_path), None, [_ci("alpha-x", "alpha")])
    run = _fake_account_cli(mocker, listing=_groups_listing("[]"))
    render = mocker.patch.object(dashboard, "render", wraps=dashboard.render)

    # none, Follow the repo's group, New group…
    keys = [*_open_container_group_picker(group), *[b"j"] * 2, _ENTER, *_keys("fresh"), _ENTER]
    assert _drive_run(mocker, keys, [group]) == 0

    prompts = _rendered(render, dashboard.TextPrompt)
    assert {(p.purpose, p.target) for p in prompts} == {("container-group-name", "alpha-x")}
    assert run.call_args_list[-1] == mocker.call(
        ["account", "group", "use", "fresh", "alpha-x"], cwd=tmp_path
    )
    prompt_frames = [
        c for c in render.call_args_list if isinstance(c.kwargs["overlay"], dashboard.TextPrompt)
    ]
    assert {c.args[1] for c in prompt_frames} == {dashboard.Row("container", "alpha-x")}


@pytest.mark.parametrize(
    "tail",
    [
        [_ESC],
        [b"\x03"],
        [*[b"j"] * 2, _ENTER, *_keys("fresh"), _ESC],
        [*[b"j"] * 2, _ENTER, *_keys("fresh"), b"\x03"],
    ],
    ids=["esc-at-picker", "ctrl-c-at-picker", "esc-at-name-prompt", "ctrl-c-at-name-prompt"],
)
def test_container_credential_group_esc_runs_nothing(mocker, tmp_path, tail):
    group = dashboard.RepoGroup("alpha", str(tmp_path), None, [_ci("alpha-x", "alpha")])
    run = _fake_account_cli(mocker, listing=_groups_listing("[]"))
    child = mocker.patch.object(dashboard.subprocess, "run")
    render = mocker.patch.object(dashboard, "render", wraps=dashboard.render)

    assert _drive_run(mocker, [*_open_container_group_picker(group), *tail], [group]) == 0

    assert run.call_args_list == [mocker.call(dashboard.da.group_ls_argv(), cwd=tmp_path)]
    child.assert_not_called()
    assert _rendered(render, dashboard.Picker)
    assert render.call_args_list[-1].kwargs["overlay"] is None


@pytest.mark.parametrize("when", ["frame-before-enter", "same-read-as-enter"])
def test_container_vanishing_while_the_group_picker_is_open_runs_nothing(mocker, tmp_path, when):
    """Closed by the loop-top guard, or refused by the submit's own re-resolve."""
    group = dashboard.RepoGroup("alpha", str(tmp_path), None, [_ci("alpha-x", "alpha")])
    run = _fake_account_cli(mocker, listing=_groups_listing(_TEAM_ROWS))
    child = mocker.patch.object(dashboard.subprocess, "run")
    render = mocker.patch.object(dashboard, "render", wraps=dashboard.render)
    vanish = [*_open_container_group_picker(group), "vanish"]
    script = iter([*vanish, _ENTER] if when == "frame-before-enter" else vanish)

    def read(_fd, _n):
        item = next(script, b"\x03")
        if item == "vanish":
            group.containers.clear()
            return b"z" if when == "frame-before-enter" else _ENTER
        return item

    assert _drive_run_with_reader(mocker, read, [group]) == 0

    assert run.call_args_list == [mocker.call(dashboard.da.group_ls_argv(), cwd=tmp_path)]
    child.assert_not_called()
    notices = " ".join(str(c.kwargs["notice"]) for c in render.call_args_list)
    assert "'alpha-x' is gone" in notices


def test_container_group_flow_is_not_misdirected_by_a_repo_of_the_same_name(mocker, tmp_path):
    """Container `alpha-x` of repo `alpha` beside a repo whose prefix is `alpha-x`."""
    alpha_root, other_root = tmp_path / "alpha", tmp_path / "other"
    group = dashboard.RepoGroup("alpha", str(alpha_root), None, [_ci("alpha-x", "alpha")])
    namesake = dashboard.RepoGroup(
        "alpha-x", str(other_root), None, [_ci("alpha-x-one", "alpha-x")]
    )
    run = _fake_account_cli(mocker, listing=_groups_listing(_TEAM_ROWS))
    render = mocker.patch.object(dashboard, "render", wraps=dashboard.render)

    keys = [*_open_container_group_picker(group), _ENTER]  # "team"
    assert _drive_run(mocker, keys, [group, namesake]) == 0

    assert run.call_args_list == [
        mocker.call(dashboard.da.group_ls_argv(), cwd=alpha_root),
        mocker.call(["account", "group", "use", "team", "alpha-x"], cwd=alpha_root),
    ]
    picker_frames = [
        c for c in render.call_args_list if isinstance(c.kwargs["overlay"], dashboard.Picker)
    ]
    assert picker_frames
    assert {c.args[1] for c in picker_frames} == {dashboard.Row("container", "alpha-x")}


def test_new_from_a_container_row_leaves_the_cursor_there_after_esc(mocker, tmp_path):
    group = dashboard.RepoGroup(
        "alpha", str(tmp_path), None, [_ci("alpha-x", "alpha"), _ci("alpha-y", "alpha")]
    )
    render = mocker.patch.object(dashboard, "render", wraps=dashboard.render)
    mocker.patch.object(dashboard, "new_container_base_default", return_value="main")

    assert _drive_run(mocker, [b"j", b"j", b"n", *_keys("fe"), _ESC], [group]) == 0

    calls = render.call_args_list
    prompt_frames = [c for c in calls if isinstance(c.kwargs["overlay"], dashboard.TextPrompt)]
    assert prompt_frames
    assert {c.args[1] for c in prompt_frames} == {dashboard.Row("container", "alpha-y")}
    assert calls[-1].kwargs["overlay"] is None
    assert calls[-1].args[1] == dashboard.Row("container", "alpha-y")


def test_run_reports_a_vanished_repo_root_instead_of_crashing(mocker, tmp_path):
    """Task 10b's dispatch runs the child with ``cwd=<repo root>``. If that
    directory disappears between a refresh and this keypress,
    ``subprocess.run`` raises rather than exiting non-zero, and — before this
    fix — nothing in `run`'s key loop caught it: the whole TUI went down with
    a traceback. Drives the real `dispatch` closure (not just
    `_dispatch_action` in isolation) so a regression in the `try/except`
    wrapped around it is what this test actually exercises.
    """
    group = dashboard.RepoGroup("alpha", str(tmp_path), None, [_ci("alpha-x", "alpha")])
    mocker.patch.object(dashboard.subprocess, "run", side_effect=OSError("gone"))
    render = mocker.patch.object(dashboard, "render", wraps=dashboard.render)

    # "j" moves the highlight off the repo header onto the container row;
    # "t" (tmux) is offered for a Running container and dispatches through
    # `run`'s real `dispatch`, not `run_new_container`'s separate path.
    rc = _drive_run(mocker, [b"j", b"t"], groups=[group])

    assert rc == 0  # run() returned normally — the OSError did not propagate
    notices = [call.kwargs.get("notice") for call in render.call_args_list]
    assert any(n is not None and str(tmp_path) in n for n in notices)


def test_foreground_marks_the_client_inactive_and_refreshes_on_return(mocker, tmp_path):
    """A dashboard that launched `jb tmux` must stop the shared service
    gathering on its behalf, and show fresh state when the user comes back."""
    group = dashboard.RepoGroup("alpha", str(tmp_path), None, [_ci("alpha-x", "alpha")])
    during: list[list[tuple]] = []

    def child(*args, **kwargs):
        during.append(list(fake.events))
        return mocker.Mock(returncode=0)

    fake = _fake_state(mocker, [group])
    mocker.patch.object(dashboard.subprocess, "run", side_effect=child)
    _mock_terminal(mocker)
    mocker.patch.object(dashboard.select, "select", return_value=([True], [], []))
    keys = itertools.chain([b"j", b"t"], itertools.repeat(b"\x03"))
    mocker.patch.object(dashboard.os, "read", side_effect=lambda fd, n: next(keys))

    assert dashboard.run(mocker.Mock(), None) == 0
    assert during and during[0][-1] == ("active", False)
    after = fake.events[len(during[0]) :]
    assert after[:2] == [("active", True), ("refresh",)]


def test_a_service_problem_shows_as_a_notice_without_exiting(mocker, tmp_path):
    group = dashboard.RepoGroup("alpha", str(tmp_path), None, [_ci("alpha-x", "alpha")])
    render = mocker.patch.object(dashboard, "render", wraps=dashboard.render)
    _mock_terminal(mocker)
    _fake_state(mocker, [group], status="refresh failed: incus is down")
    mocker.patch.object(dashboard.select, "select", return_value=([True], [], []))
    keys = itertools.chain([b"j", b"j"], itertools.repeat(b"\x03"))
    mocker.patch.object(dashboard.os, "read", side_effect=lambda fd, n: next(keys))

    assert dashboard.run(mocker.Mock(), None) == 0
    assert any(
        call.kwargs.get("notice") == "refresh failed: incus is down"
        for call in render.call_args_list
    )


def test_the_r_key_asks_the_service_for_a_refresh(mocker):
    fake = _fake_state(mocker, [])
    _mock_terminal(mocker)
    mocker.patch.object(dashboard.select, "select", return_value=([True], [], []))
    keys = itertools.chain([b"r"], itertools.repeat(b"\x03"))
    mocker.patch.object(dashboard.os, "read", side_effect=lambda fd, n: next(keys))

    assert dashboard.run(mocker.Mock(), None) == 0
    assert ("refresh",) in fake.events
    assert fake.closed


def test_run_pins_its_own_cwd_and_applies_its_scope(mocker, tmp_path):
    """Neither is baked into the shared snapshot: every frame, the dashboard
    hands its own ``cwd_root`` and ``scope`` to `present`."""
    from jailbee.remote_ssh.repo_scope import RemoteRepoScope

    a = dashboard.RepoGroup("alpha", "/a", None, [])
    b = dashboard.RepoGroup("beta", str(tmp_path), None, [])
    s = dashboard.RepoGroup("secret", "/s", None, [])
    render = mocker.patch.object(dashboard, "render", wraps=dashboard.render)
    _mock_terminal(mocker)
    _fake_state(mocker, [a, b, s])
    mocker.patch.object(dashboard.select, "select", return_value=([True], [], []))
    mocker.patch.object(dashboard.os, "read", return_value=b"\x03")

    dashboard.run(mocker.Mock(), tmp_path, scope=RemoteRepoScope(frozenset({"secret"})))

    shown = render.call_args_list[-1].args[0]
    assert [g.prefix for g in shown] == ["beta", "alpha"]


def test_inline_command_on_repo_header_leaves_merge_source_for_cli(mocker, tmp_path):
    group = dashboard.RepoGroup("alpha", str(tmp_path), None, [_ci("alpha-x", "alpha")])
    run = mocker.patch.object(dashboard.subprocess, "run")
    run.return_value.returncode = 0
    wait = mocker.patch.object(dashboard, "_wait_for_return")

    rc = _drive_run(mocker, [b"!", b"merge", b"\r"], groups=[group])

    assert rc == 0
    run.assert_called_once_with(["jailbee", "merge"], cwd=tmp_path, check=False)
    wait.assert_called_once()


def test_inline_command_on_container_uses_selected_source(mocker, tmp_path):
    group = dashboard.RepoGroup("alpha", str(tmp_path), None, [_ci("alpha-x", "alpha")])
    run = mocker.patch.object(dashboard.subprocess, "run")
    run.return_value.returncode = 0
    mocker.patch.object(dashboard, "_wait_for_return")

    rc = _drive_run(mocker, [b"j", b"!", b"merge", b"\r"], groups=[group])

    assert rc == 0
    run.assert_called_once_with(["jailbee", "merge", "alpha-x"], cwd=tmp_path, check=False)


def test_inline_command_malformed_quote_notifies_without_spawning(mocker, tmp_path):
    group = dashboard.RepoGroup("alpha", str(tmp_path), None, [_ci("alpha-x", "alpha")])
    run = mocker.patch.object(dashboard.subprocess, "run")
    render = mocker.patch.object(dashboard, "render", wraps=dashboard.render)

    _drive_run(mocker, [b"j", b"!", b"merge '", b"\r"], groups=[group])

    run.assert_not_called()
    assert any("cannot parse command" in str(c.kwargs.get("notice")) for c in render.call_args_list)


def test_inline_command_refuses_orphan_before_spawning(mocker):
    group = dashboard.RepoGroup("alpha", None, None, [_ci("alpha-x", "alpha")])
    run = mocker.patch.object(dashboard.subprocess, "run")
    render = mocker.patch.object(dashboard, "render", wraps=dashboard.render)

    _drive_run(mocker, [b"j", b"!", b"merge", b"\r"], groups=[group])

    run.assert_not_called()
    assert any("view-only" in str(c.kwargs.get("notice")) for c in render.call_args_list)


def test_inline_command_refuses_without_selection_before_spawning(mocker):
    run = mocker.patch.object(dashboard.subprocess, "run")
    render = mocker.patch.object(dashboard, "render", wraps=dashboard.render)

    _drive_run(mocker, [b"!", b"merge", b"\r"])

    run.assert_not_called()
    assert any(
        "Select a repo or a container" in str(c.kwargs.get("notice")) for c in render.call_args_list
    )


def test_inline_command_refuses_ssh_policy_before_foreground_or_spawn(mocker, tmp_path):
    from jailbee.config.models_remote import RemoteCommandPolicy, RemoteSSHConfig

    group = dashboard.RepoGroup("alpha", str(tmp_path), None, [_ci("alpha-x", "alpha")])
    run = mocker.patch.object(dashboard.subprocess, "run")
    wait = mocker.patch.object(dashboard, "_wait_for_return")
    render = mocker.patch.object(dashboard, "render", wraps=dashboard.render)
    _mock_terminal(mocker)
    _fake_state(mocker, [group])
    mocker.patch.object(dashboard.select, "select", return_value=([True], [], []))
    keys = itertools.chain([b"j", b"!", b"merge", b"\r", b"\x03"], itertools.repeat(b"\x03"))
    mocker.patch.object(dashboard.os, "read", side_effect=lambda fd, n: next(keys))
    policy = RemoteSSHConfig(
        exec=True, commands=RemoteCommandPolicy(mode="allowlist", allow=["git pull"])
    )

    dashboard.run(
        mocker.Mock(),
        None,
        remote=True,
        over_ssh=True,
        ssh_policy=policy,
    )

    run.assert_not_called()
    wait.assert_not_called()
    assert any("not allowed" in str(c.kwargs.get("notice")) for c in render.call_args_list)


def test_inline_command_reports_vanished_repo_and_returns_to_loop(mocker, tmp_path):
    group = dashboard.RepoGroup("alpha", str(tmp_path), None, [_ci("alpha-x", "alpha")])
    mocker.patch.object(dashboard.subprocess, "run", side_effect=OSError("gone"))
    render = mocker.patch.object(dashboard, "render", wraps=dashboard.render)

    rc = _drive_run(mocker, [b"j", b"!", b"merge", b"\r"], groups=[group])

    assert rc == 0
    assert any(str(tmp_path) in str(c.kwargs.get("notice")) for c in render.call_args_list)


def test_q_inside_inline_editor_is_text_and_does_not_quit(mocker):
    render = mocker.patch.object(dashboard, "render", wraps=dashboard.render)

    _drive_run(mocker, [b"!", b"q", b"\x1b", b"q"])

    assert any(
        isinstance(c.kwargs.get("overlay"), dashboard.CommandState)
        and c.kwargs["overlay"].text == "q"
        for c in render.call_args_list
    )


def test_new_container_reports_a_vanished_repo_root_instead_of_crashing(mocker, tmp_path):
    """The identical failure as the test above, reached through a different
    keypress: once both inline answers are in, `run_new_container`'s own
    `subprocess.run(new_container_argv(...), cwd=repo.cwd())` raises the same
    uncaught `OSError` if the repo root disappeared between a refresh and the
    final Enter. Exercises `_report_vanished_repo`'s other call site (shared
    with `dispatch`) rather than assuming the fix generalizes.
    """
    group = dashboard.RepoGroup("alpha", str(tmp_path), None, [_ci("alpha-x", "alpha")])
    # Patches the same `subprocess.run` `new_container_base_default` calls
    # through `git.get_current_branch` — that call already tolerates OSError
    # and returns None (so the base field opens empty), so this only affects
    # `run_new_container`'s subprocess.run (see git.get_current_branch's own
    # try/except).
    mocker.patch.object(dashboard.subprocess, "run", side_effect=OSError("gone"))
    render = mocker.patch.object(dashboard, "render", wraps=dashboard.render)

    # The repo header row is selected by default (no navigation needed): "n"
    # opens the branch prompt, then the base prompt, and the final Enter runs
    # `jailbee new` through `run_new_container`, not `dispatch`.
    keys = [b"n", *_keys("work"), _ENTER, *_keys("main"), _ENTER]
    rc = _drive_run(mocker, keys, groups=[group])

    assert rc == 0  # run() returned normally — the OSError did not propagate
    notices = [call.kwargs.get("notice") for call in render.call_args_list]
    assert any(n is not None and str(tmp_path) in n for n in notices)


def test_run_dispatches_e_and_shift_e_to_edit_config(mocker, tmp_path):
    """Drive `e`/`E` through `run()`'s real dispatch (``elif key in
    ("config-edit", "config-edit-global"): edit_config(...)``), not just
    `parse_key`/the binding shape in isolation — a wrong key comparison or an
    inverted ``global_layer`` would be caught by nothing else.

    Asserts on the argv each keypress actually spawns (``--global`` present
    or absent), not merely that ``edit_config`` was reached, so an inverted
    ``global_layer`` fails this test rather than sailing through it.
    """
    group = dashboard.RepoGroup(
        "alpha", str(tmp_path), tmp_path / ".jailbee" / "config.yaml", [_ci("alpha-x", "alpha")]
    )
    run = mocker.patch.object(dashboard.subprocess, "run")
    run.return_value.returncode = 0

    rc = _drive_run(mocker, [b"e", b"E"], groups=[group])

    assert rc == 0
    argvs = [call.args[0] for call in run.call_args_list]
    assert len(argvs) == 2
    assert argvs[0][:3] == ["jailbee", "config", "edit"]
    assert "--global" not in argvs[0]
    assert "--global" in argvs[1]


def test_remote_run_never_opens_the_config_editor(mocker, tmp_path):
    """Over remote SSH, `e`/`E` would hand the client an editor for host
    mounts and for `remote.ssh` itself — the policy that is meant to bound
    that very client. Nothing is spawned; a notice says why."""
    group = dashboard.RepoGroup(
        "alpha", str(tmp_path), tmp_path / ".jailbee" / "config.yaml", [_ci("alpha-x", "alpha")]
    )
    run = mocker.patch.object(dashboard.subprocess, "run")
    render = mocker.patch.object(dashboard, "render", wraps=dashboard.render)

    rc = _drive_run(mocker, [b"e", b"E"], groups=[group], remote=True)

    assert rc == 0
    run.assert_not_called()
    notices = [call.kwargs.get("notice") for call in render.call_args_list]
    assert dashboard.REMOTE_CONFIG_EDIT_NOTE in notices


def test_edit_config_reports_a_vanished_repo_root_instead_of_crashing(mocker, tmp_path):
    """The identical failure as ``test_run_reports_a_vanished_repo_root_instead_
    of_crashing`` and ``test_new_container_reports_a_vanished_repo_root_
    instead_of_crashing``, reached through the config-edit keypress:
    `edit_config`'s own ``subprocess.run(argv, cwd=repo.cwd())`` raises the
    same uncaught `OSError` if the repo root disappeared between a refresh
    and "e". Exercises `_report_vanished_repo`'s third call site rather than
    assuming the fix generalizes.
    """
    group = dashboard.RepoGroup(
        "alpha", str(tmp_path), tmp_path / ".jailbee" / "config.yaml", [_ci("alpha-x", "alpha")]
    )
    mocker.patch.object(dashboard.subprocess, "run", side_effect=OSError("gone"))
    render = mocker.patch.object(dashboard, "render", wraps=dashboard.render)

    rc = _drive_run(mocker, [b"e"], groups=[group])

    assert rc == 0  # run() returned normally — the OSError did not propagate
    notices = [call.kwargs.get("notice") for call in render.call_args_list]
    assert any(n is not None and str(tmp_path) in n for n in notices)


def test_parse_key_maps_n_to_the_new_container_token():
    assert dashboard.parse_key(b"n") == "new"


def test_new_binding_is_not_a_container_verb():
    """`n` is repo-scoped. A `verb` would put it through `quick_verb`, which
    gates on a *container's* state and would reject it everywhere."""
    binding = dashboard.binding_for_token("new")
    assert binding is not None
    assert binding.verb is None


def test_new_binding_appears_in_the_help_overlay(tmp_path):
    g = dashboard.RepoGroup(
        "alpha", "/repos/alpha", tmp_path / "a.yaml", [_ci("alpha-one", "alpha")]
    )
    out = _render_text(
        dashboard.render(
            [g],
            selected=None,
            now=datetime(2026, 6, 8, 12, 0, tzinfo=UTC),
            git_enabled=True,
            overlay="help",
        )
    )
    assert "create a container" in out


def test_e_and_shift_e_are_bound_and_documented():
    from jailbee.dashboard import KEY_BINDINGS, parse_key

    assert parse_key(b"e") == "config-edit"
    assert parse_key(b"E") == "config-edit-global"
    tokens = {b.token for b in KEY_BINDINGS}
    assert {"config-edit", "config-edit-global"} <= tokens
    # The pair documents itself once, the way up/down does.
    hints = [b.hint for b in KEY_BINDINGS if b.token.startswith("config-edit")]
    assert hints.count("") == 1


def test_config_edit_reject_note_names_configuring_not_creating():
    from jailbee.dashboard import config_edit_reject_note_for_prefix

    assert config_edit_reject_note_for_prefix([], "") == "Select a repo or a container first"
    group = dashboard.RepoGroup(prefix="orphan", repo_root=None, config_path=None, containers=[])
    note = config_edit_reject_note_for_prefix([group], "orphan")
    assert note is not None
    assert "config" in note
    assert "create" not in note


def test_config_edit_reject_note_accepts_a_real_repo(tmp_path):
    from jailbee.dashboard import config_edit_reject_note_for_prefix

    group = dashboard.RepoGroup(
        prefix="demo",
        repo_root=str(tmp_path),
        config_path=tmp_path / ".jailbee" / "config.yaml",
        containers=[],
    )
    assert config_edit_reject_note_for_prefix([group], "demo") is None


def test_config_edit_reject_note_refuses_the_repo_layer_of_a_synthesized_config(tmp_path):
    """A repo with no config file of its own has a third layer the editor
    cannot see (``global.yaml``'s ``scratch.config``), and the first save
    would stop that layer being used at all — so ``e`` is refused there.

    ``E`` is not: it edits ``global.yaml``, which is where that directory's
    settings actually live.
    """
    from jailbee.dashboard import config_edit_reject_note_for_prefix

    group = dashboard.RepoGroup(
        prefix="demo", repo_root=str(tmp_path), config_path=None, containers=[]
    )

    note = config_edit_reject_note_for_prefix([group], "demo")

    assert note is not None
    assert "scratch.config" in note
    assert "config init" in note
    assert config_edit_reject_note_for_prefix([group], "demo", global_layer=True) is None


def test_sample_activity_flattens_every_group(mocker):
    """One reading covers the whole screen — not one per repo group."""
    annotate = mocker.patch.object(dashboard, "annotate_activity")
    # `_ci(name, repo, ...)` — two positional arguments, see its definition
    # near the top of this test module.
    a = _ci("p-a", "p")
    b = _ci("q-b", "q")
    groups = [
        dashboard.RepoGroup("p", "/p", Path("/p/.jailbee/config.yaml"), [a]),
        dashboard.RepoGroup("q", "/q", Path("/q/.jailbee/config.yaml"), [b]),
    ]
    sampler = mocker.Mock()

    dashboard.sample_activity(groups, sampler)

    annotate.assert_called_once_with([a, b], sampler)


def test_sample_activity_matches_agents_per_group_never_across_repos(mocker):
    """Each group reads its own containers' session homes, from one sampler reading."""
    mocker.patch.object(dashboard, "annotate_activity")
    agents = mocker.patch.object(dashboard, "annotate_agent_status")
    p_home = (("p-a", "claude", Path("/s/p/.private/p-a/claude")),)
    q_home = (("q-b", "claude", Path("/s/q/.private/q-b/claude")),)
    by_homes = {p_home: {"p-a": ["p-session"]}, q_home: {"q-b": ["q-session"]}}
    mocker.patch("jailbee.agent_status.read_sessions", side_effect=lambda h: by_homes[tuple(h)])
    a, b = _ci("p-a", "p"), _ci("q-b", "q")
    groups = [
        dashboard.RepoGroup("p", "/p", None, [a], agent_homes=p_home),
        dashboard.RepoGroup("q", "/q", None, [b], agent_homes=q_home),
    ]
    sampler = mocker.Mock()

    dashboard.sample_activity(groups, sampler)

    assert [c.args for c in agents.call_args_list] == [
        ([a], {"p-a": ["p-session"]}, sampler),
        ([b], {"q-b": ["q-session"]}, sampler),
    ]


def test_sample_activity_gives_each_group_a_lookup_over_its_own_config_homes(mocker):
    from jailbee.agent_activity import ActivityReader

    mocker.patch.object(dashboard, "annotate_activity")
    agents = mocker.patch.object(dashboard, "annotate_agent_status")
    mocker.patch("jailbee.agent_status.read_sessions", return_value={})
    reader = mocker.Mock(spec=ActivityReader)
    reader.lookup_for.side_effect = lambda homes, processes: ("lookup", dict(homes))
    p_homes = (("p-a", "claude", Path("/s/p/claude")),)
    groups = [
        dashboard.RepoGroup("p", "/p", None, [_ci("p-a", "p")], agent_config_homes=p_homes),
        dashboard.RepoGroup("q", "/q", None, [_ci("q-b", "q")]),
    ]
    sampler = mocker.Mock()

    dashboard.sample_activity(groups, sampler, reader)

    assert [c.kwargs["activity"] for c in agents.call_args_list] == [
        ("lookup", {("p-a", "claude"): Path("/s/p/claude")}),
        ("lookup", {}),
    ]
    reader.begin.assert_called_once_with()
    reader.finish.assert_called_once_with()
    assert reader.lookup_for.call_args_list[0].args[1] == sampler.processes


def test_sample_activity_without_a_reader_asks_for_no_activity(mocker):
    mocker.patch.object(dashboard, "annotate_activity")
    agents = mocker.patch.object(dashboard, "annotate_agent_status")
    mocker.patch("jailbee.agent_status.read_sessions", return_value={})
    groups = [dashboard.RepoGroup("p", "/p", None, [_ci("p-a", "p")])]

    dashboard.sample_activity(groups, mocker.Mock())

    assert agents.call_args.kwargs == {"activity": None}


def test_a_failing_group_still_lets_the_reader_finish(mocker):
    from jailbee.agent_activity import ActivityReader

    mocker.patch.object(dashboard, "annotate_activity")
    mocker.patch.object(dashboard, "annotate_agent_status", side_effect=RuntimeError("boom"))
    mocker.patch("jailbee.agent_status.read_sessions", return_value={})
    reader = mocker.Mock(spec=ActivityReader)
    groups = [dashboard.RepoGroup("p", "/p", None, [_ci("p-a", "p")])]

    dashboard.sample_activity(groups, mocker.Mock(), reader)

    reader.finish.assert_called_once_with()


def test_a_failing_lookup_still_lets_the_reader_finish(mocker):
    """`lookup_for` runs outside the per-group guard, so only `finally` closes the tick."""
    from jailbee.agent_activity import ActivityReader

    mocker.patch.object(dashboard, "annotate_activity")
    mocker.patch.object(dashboard, "annotate_agent_status")
    reader = mocker.Mock(spec=ActivityReader)
    reader.lookup_for.side_effect = RuntimeError("boom")
    groups = [dashboard.RepoGroup("p", "/p", None, [_ci("p-a", "p")])]

    with pytest.raises(RuntimeError):
        dashboard.sample_activity(groups, mocker.Mock(), reader)

    reader.finish.assert_called_once_with()


def test_sample_activity_survives_a_group_whose_agent_reading_fails(mocker):
    """One group's failure clears that group's AGENT and leaves the others."""
    mocker.patch.object(dashboard, "annotate_activity")
    agents = mocker.patch.object(
        dashboard, "annotate_agent_status", side_effect=[RuntimeError("boom"), None]
    )
    mocker.patch("jailbee.agent_status.read_sessions", return_value={})
    a, b = _ci("p-a", "p"), _ci("q-b", "q")
    a.agent_status = (mocker.Mock(),)
    groups = [
        dashboard.RepoGroup("p", "/p", None, [a]),
        dashboard.RepoGroup("q", "/q", None, [b]),
    ]

    dashboard.sample_activity(groups, mocker.Mock())

    assert agents.call_count == 2
    assert a.agent_status == ()


def test_sample_activity_reads_the_agent_state_after_the_activity_reading(mocker):
    """AGENT uses the reading `annotate_activity` just took."""
    order: list[str] = []
    mocker.patch.object(
        dashboard, "annotate_activity", side_effect=lambda *a: order.append("activity")
    )
    mocker.patch.object(
        dashboard, "annotate_agent_status", side_effect=lambda *a, **k: order.append("agent")
    )
    groups = [dashboard.RepoGroup("p", "/p", None, [_ci("p-a", "p")])]

    dashboard.sample_activity(groups, mocker.Mock())

    assert order == ["activity", "agent"]


def test_remote_action_menu_never_opens_the_pr_in_a_host_browser():
    local = dashboard.menu_actions(_ctx(pr_number=7))
    remote = dashboard.menu_actions(_ctx(pr_number=7, remote=True))

    assert ("Open PR", "pr --open") in local
    assert all(verb != "pr --open" for _label, verb in remote)


def test_unrestricted_ssh_dashboard_is_registered_only_but_not_restricted(mocker, monkeypatch):
    """`restrict_host: false` lifts the restrictions, not the facts of being
    remote: the server's cwd is still no repo, the setup offer is still for
    someone at the host, and a Qt window would still open on its display."""
    monkeypatch.delenv("JAILBEE_REMOTE_SSH", raising=False)
    monkeypatch.setenv("JAILBEE_SSH_SESSION", "1")
    monkeypatch.setenv("JAILBEE_SSH_EXCLUDED_REPOS", "[]")
    load = mocker.patch("jailbee.config.load_repo_config")
    advise = mocker.patch("jailbee.cli._advise_setup")
    run = mocker.patch("jailbee.dashboard.run", return_value=0)
    mocker.patch("jailbee.incus.Incus")

    from jailbee.config.models_remote import RemoteCommandPolicy, RemoteSSHConfig

    policy_json = RemoteSSHConfig(
        exec=True,
        restrict_host=False,
        commands=RemoteCommandPolicy(mode="full"),
    ).model_dump_json()
    assert (
        CliRunner().invoke(app, ["dashboard", "--remote-policy-json", policy_json]).exit_code == 0
    )
    load.assert_not_called()
    advise.assert_not_called()
    assert run.call_args.kwargs["cwd_root"] is None
    assert run.call_args.kwargs["remote"] is False
    assert CliRunner().invoke(app, ["gui"]).exit_code == 2


def test_group_menu_actions_terminal_order_hoists_pending_and_puts_git_first():
    leaves = [
        ("Attach tmux", "tmux"),
        ("Open shell", "shell"),
        ("Open PR", "pr --open"),
        ("Create/update PR", "pr"),
        ("Apply 2 PR action(s) (review apply)", "review apply"),
        ("Merge into…", "merge"),
        ("Update from base (git push)", "git push"),
        ("Apply 1 issue action(s) (issue apply)", "issue apply"),
        ("Network: loose", "net loose"),
    ]
    assert dashboard.group_menu_actions(leaves, include_network=True, terminal_order=True) == [
        leaves[4],
        leaves[7],
        leaves[0],
        dashboard.MenuGroup("Git →", (leaves[5], leaves[6])),
        dashboard.MenuGroup("PR →", (leaves[2], leaves[3])),
        dashboard.MenuGroup("Network →", (leaves[8],)),
    ]


def test_terminal_order_running_menu_layout(tmp_path):
    apps = [dashboard.AppMenuEntry("chrome", "Chrome")]
    group = dashboard.RepoGroup(
        "alpha", str(tmp_path), None, [_ci("alpha-x", "alpha", pr_number=7)], apps=apps
    )
    menu = dashboard.open_menu([group], "alpha-x")
    assert menu is not None
    labels = [
        item.label if isinstance(item, dashboard.MenuGroup) else item[0]
        for item in dashboard._menu_entries(menu)
    ]
    assert labels[:7] == [
        "Attach tmux",
        "Launch →",
        "Outbox",
        "Git →",
        "PR →",
        "Lifecycle →",
        "Network →",
    ]
    assert "Open shell" not in labels
    lifecycle = next(
        i
        for i in dashboard._menu_entries(menu)
        if isinstance(i, dashboard.MenuGroup) and i.label == "Lifecycle →"
    )
    assert [v for _, v in lifecycle.actions] == ["restart", "stop", "destroy"]
    # Leaves stay offered: the `s` and `D` keys gate on them.
    assert {"shell", "destroy"} <= {v for _, v in menu.actions}


def test_terminal_order_stopped_menu_keeps_a_lone_destroy_leaf(tmp_path):
    group = dashboard.RepoGroup(
        "alpha", str(tmp_path), None, [_ci("alpha-x", "alpha", "Stopped", pr_number=7)]
    )
    menu = dashboard.open_menu([group], "alpha-x")
    assert menu is not None
    labels = [
        item.label if isinstance(item, dashboard.MenuGroup) else item[0]
        for item in dashboard._menu_entries(menu)
    ]
    assert labels[0] == "Start"
    at = labels.index("PR →")
    assert labels[at : at + 3] == ["PR →", "Destroy", "Network →"]
    assert "Lifecycle →" not in labels


def test_group_menu_actions_default_order_is_unchanged_for_qt():
    leaves = [
        ("Create/update PR", "pr"),
        ("Apply 2 PR action(s) (review apply)", "review apply"),
        ("Merge into…", "merge"),
    ]
    assert dashboard.group_menu_actions(leaves) == [
        dashboard.MenuGroup("PR →", (leaves[0], leaves[1])),
        dashboard.MenuGroup("Git →", (leaves[2],)),
    ]


def test_terminal_menu_drops_an_empty_pr_group_when_only_apply_remains():
    # Mount mode: no Create/update PR, only the pending apply — the apply is
    # hoisted and the PR → group must not survive as an empty shell.
    actions = dashboard.menu_actions(_ctx(mode="mount", git_status=_dirty(pending_pr_actions=2)))
    menu = dashboard.MenuState("alpha-x", actions)
    labels = [
        item.label if isinstance(item, dashboard.MenuGroup) else item[0]
        for item in dashboard._menu_entries(menu)
    ]
    assert labels[0] == "Outbox (2 pending)"
    assert "PR →" not in labels


# --- The Accounts panel (A) --------------------------------------------------

# Same rows as `ROWS` in tests/test_dashboard_accounts.py: a live login in
# "team", a parked login, an empty "spare" group.
_ACCOUNT_ROWS = (
    '[{"agent": "claude", "group": "team", "account": "a@x.io#org12345", "state": "live",'
    ' "repos": ["alpha"], "containers": ["alpha-x"]},'
    ' {"agent": "claude", "group": null, "account": "b@x.io~2", "state": "parked",'
    ' "repos": [], "containers": []},'
    ' {"agent": "claude", "group": "spare", "account": null, "state": "empty",'
    ' "repos": [], "containers": []}]'
)
_ACCOUNT_LS = dashboard.da.account_ls_argv()


def _fake_accounts_cli(mocker, *, listing=None, change=None):
    """Patch the quiet CLI runner for the Accounts panel.

    Every `account ls` answers ``listing``; anything else is a change and
    answers ``change``.
    """
    listing = listing or _groups_listing(_ACCOUNT_ROWS)
    change = change or dashboard.da.CliResult(True, "Done.")

    def fake(argv, **_kwargs):
        return listing if argv[:2] == ["account", "ls"] else change

    return mocker.patch.object(dashboard.da, "run_cli_quiet", side_effect=fake)


def _alpha(tmp_path):
    return dashboard.RepoGroup("alpha", str(tmp_path), None, [_ci("alpha-x", "alpha")])


def test_key_a_opens_the_accounts_panel_with_rows_and_keeps_the_table(mocker, tmp_path):
    group = dashboard.RepoGroup("alpha", str(tmp_path), None, [_ci("alpha-zebra", "alpha")])
    run = _fake_accounts_cli(mocker)
    child = mocker.patch.object(dashboard.subprocess, "run")
    render = mocker.patch.object(dashboard, "render", wraps=dashboard.render)

    assert _drive_run(mocker, [b"A"], [group]) == 0

    assert run.call_args_list == [mocker.call(_ACCOUNT_LS, cwd=tmp_path)]
    child.assert_not_called()
    frames = [
        c
        for c in render.call_args_list
        if isinstance(c.kwargs["overlay"], dashboard.da.AccountsState)
    ]
    assert frames, "the Accounts panel was never drawn"
    state = frames[-1].kwargs["overlay"]
    assert [r.account for r in state.rows] == ["a@x.io#org12345", "b@x.io~2", None]
    assert (state.index, state.prefix) == (0, "alpha")
    out = _render_text(dashboard.render(*frames[-1].args, **frames[-1].kwargs))
    assert "NAME" in out and "zebra" in out  # the container table is still drawn
    assert "credential groups and logins" in out
    assert "b@x.io~2" in out
    assert "n new group" in out  # the panel's own hint line


def test_repo_menu_offers_accounts_after_the_credential_group():
    group = dashboard.RepoGroup("alpha", "/alpha", None, [])
    menu = dashboard.open_repo_menu([group], "alpha", frozenset())
    assert menu is not None
    assert menu.actions[3] == ("Accounts…", "accounts")


@pytest.mark.parametrize(
    ("over_ssh", "policy_kwargs", "offered"),
    [
        (False, {"commands": {"mode": "disabled"}}, True),
        (True, {"commands": {"mode": "allowlist", "allow": ["new", "tmux"]}}, False),
    ],
    ids=["local-ignores-policy", "ssh-allowlist-without-it"],
)
def test_repo_menu_accounts_follows_the_ssh_policy(over_ssh, policy_kwargs, offered):
    group = dashboard.RepoGroup("alpha", "/alpha", None, [])
    menu = dashboard.open_repo_menu(
        [group], "alpha", frozenset(), ssh_policy=_ssh_policy(policy_kwargs), over_ssh=over_ssh
    )
    assert menu is not None
    verbs = [item[1] for item in menu.actions if not isinstance(item, dashboard.MenuGroup)]
    assert ("accounts" in verbs) is offered


def test_repo_menu_accounts_opens_the_panel_in_that_repo(mocker, tmp_path):
    run = _fake_accounts_cli(mocker)
    render = mocker.patch.object(dashboard, "render", wraps=dashboard.render)
    keys = [b"\r", *[b"j"] * 3, b"\r"]  # repo header → Accounts…

    assert _drive_run(mocker, keys, [_alpha(tmp_path)]) == 0

    assert run.call_args_list == [mocker.call(_ACCOUNT_LS, cwd=tmp_path)]
    states = _rendered(render, dashboard.da.AccountsState)
    assert states
    assert states[-1].prefix == "alpha"


def test_key_a_runs_the_listing_in_the_selected_rows_repo(mocker, tmp_path):
    alpha = dashboard.RepoGroup("alpha", str(tmp_path / "a"), None, [])
    beta = dashboard.RepoGroup("beta", str(tmp_path / "b"), tmp_path / "b.yaml", [])
    run = _fake_accounts_cli(mocker)

    assert _drive_run(mocker, [b"j", b"A"], [alpha, beta]) == 0

    assert run.call_args_list == [
        mocker.call([*_ACCOUNT_LS, "--config", str(tmp_path / "b.yaml")], cwd=tmp_path / "b")
    ]


def test_key_a_from_an_orphan_row_falls_back_to_the_first_real_repo(mocker, tmp_path):
    orphan = dashboard.RepoGroup("gamma", None, None, [_ci("gamma-x", "gamma")])
    beta = dashboard.RepoGroup("beta", str(tmp_path), None, [])
    run = _fake_accounts_cli(mocker)
    render = mocker.patch.object(dashboard, "render", wraps=dashboard.render)

    assert _drive_run(mocker, [b"A"], [orphan, beta]) == 0

    assert render.call_args_list[0].args[1] == dashboard.Row("repo", "gamma")
    assert run.call_args_list == [mocker.call(_ACCOUNT_LS, cwd=tmp_path)]
    assert _rendered(render, dashboard.da.AccountsState)[-1].prefix == "beta"


def test_key_a_with_no_real_repo_is_a_notice(mocker):
    orphan = dashboard.RepoGroup("gamma", None, None, [_ci("gamma-x", "gamma")])
    run = _fake_accounts_cli(mocker)
    render = mocker.patch.object(dashboard, "render", wraps=dashboard.render)

    assert _drive_run(mocker, [b"A"], [orphan]) == 0

    run.assert_not_called()
    last = render.call_args_list[-1].kwargs
    assert last["overlay"] is None
    assert last["notice"] == "No repo to address account commands at"


@pytest.mark.parametrize(
    ("listing", "reason"),
    [
        (dashboard.da.CliResult(False, "error: no pool"), "no pool"),
        (dashboard.da.CliResult(True, "done", "not json"), "unexpected output"),
    ],
    ids=["command-failed", "garbled-output"],
)
def test_accounts_panel_survives_a_failing_listing(mocker, tmp_path, listing, reason):
    run = _fake_accounts_cli(mocker, listing=listing)
    render = mocker.patch.object(dashboard, "render", wraps=dashboard.render)

    # j after the failure: the dashboard is still reading keys, not crashed
    assert _drive_run(mocker, [b"A", b"j"], [_alpha(tmp_path)]) == 0

    assert run.call_count == 1
    assert not _rendered(render, dashboard.da.AccountsState)
    last = render.call_args_list[-1].kwargs
    assert last["overlay"] is None
    assert last["notice"].startswith("could not list accounts: ")
    assert reason in last["notice"]
    assert render.call_args_list[-1].args[1] == dashboard.Row("container", "alpha-x")


def test_accounts_panel_with_an_empty_pool_says_so(mocker, tmp_path):
    run = _fake_accounts_cli(mocker, listing=_groups_listing("[]"))
    render = mocker.patch.object(dashboard, "render", wraps=dashboard.render)

    assert _drive_run(mocker, [b"A", _ENTER, b"j"], [_alpha(tmp_path)]) == 0

    assert run.call_count == 1  # Enter on nothing ran nothing
    last = render.call_args_list[-1]
    assert isinstance(last.kwargs["overlay"], dashboard.da.AccountsState)
    assert last.kwargs["overlay"].rows == ()
    assert last.kwargs["notice"] == "No actions for this row"
    assert "(no logins or groups on this host)" in _render_text(
        dashboard.render(*last.args, **last.kwargs)
    )
    assert not _rendered(render, dashboard.Picker)


def test_accounts_actions_picker_offers_the_rows_actions(mocker, tmp_path):
    _fake_accounts_cli(mocker)
    render = mocker.patch.object(dashboard, "render", wraps=dashboard.render)

    # row 0 (live in team), then Esc back to the panel, then row 1 (parked)
    keys = [b"A", _ENTER, _ESC, b"j", _ENTER]
    assert _drive_run(mocker, keys, [_alpha(tmp_path)]) == 0

    pickers = _rendered(render, dashboard.Picker)
    live, parked = pickers[0], pickers[-1]
    assert (live.purpose, live.title, live.target, live.carry) == (
        "acct-action",
        "Group team (claude)",
        "alpha",
        ("claude", "team", "a@x.io#org12345"),
    )
    assert [(e.label, e.value) for e in live.entries] == [
        ("Use a stored login…", "use"),
        ("Park the live login", "park"),
    ]
    assert (parked.title, parked.carry) == ("Login b@x.io~2 (claude)", ("claude", "", "b@x.io~2"))
    assert [e.value for e in parked.entries] == ["use-in", "delete"]
    assert isinstance(parked.back, dashboard.da.AccountsState)
    assert parked.back.index == 1  # the panel remembers its cursor


def test_accounts_questions_keep_the_cursor_where_the_key_was_pressed(mocker, tmp_path):
    _fake_accounts_cli(mocker)
    render = mocker.patch.object(dashboard, "render", wraps=dashboard.render)

    # j: onto the container row, then A and Enter (the actions picker)
    assert _drive_run(mocker, [b"j", b"A", _ENTER], [_alpha(tmp_path)]) == 0

    frames = [c for c in render.call_args_list if isinstance(c.kwargs["overlay"], dashboard.Picker)]
    assert frames, "the actions picker was never drawn"
    # the picker targets the repo "alpha" but must not pin its header
    assert frames[-1].args[1] == dashboard.Row("container", "alpha-x")


def test_accounts_park_runs_the_scoped_command_and_closes(mocker, tmp_path):
    run = _fake_accounts_cli(mocker, change=dashboard.da.CliResult(True, "Parked a@x.io#org12345."))
    child = mocker.patch.object(dashboard.subprocess, "run")
    render = mocker.patch.object(dashboard, "render", wraps=dashboard.render)

    # A, Enter (row 0's actions), Down to "Park the live login", Enter
    assert _drive_run(mocker, [b"A", _ENTER, b"j", _ENTER], [_alpha(tmp_path)]) == 0

    assert run.call_args_list == [
        mocker.call(_ACCOUNT_LS, cwd=tmp_path),
        mocker.call(["account", "park", "-a", "claude", "-g", "team"], cwd=tmp_path),
    ]
    child.assert_not_called()
    last = render.call_args_list[-1].kwargs
    # Done is done: the panel closes, the CLI's own message stays as the notice.
    assert last["overlay"] is None
    assert last["notice"] == "Parked a@x.io#org12345."


def test_accounts_refused_change_keeps_the_panel_under_its_notice(mocker, tmp_path):
    refusal = dashboard.da.CliResult(False, "error: an agent is running; pass --force")
    run = _fake_accounts_cli(mocker, change=refusal)
    render = mocker.patch.object(dashboard, "render", wraps=dashboard.render)

    assert _drive_run(mocker, [b"A", _ENTER, b"j", _ENTER], [_alpha(tmp_path)]) == 0

    # no reload after a refusal, and never a silent --force retry
    assert [c.args[0] for c in run.call_args_list] == [
        _ACCOUNT_LS,
        ["account", "park", "-a", "claude", "-g", "team"],
    ]
    last = render.call_args_list[-1].kwargs
    assert isinstance(last["overlay"], dashboard.da.AccountsState)
    assert len(last["overlay"].rows) == 3  # the listing it had before
    assert last["notice"] == "error: an agent is running; pass --force"


def test_accounts_use_stored_login_two_step(mocker, tmp_path):
    run = _fake_accounts_cli(mocker)
    render = mocker.patch.object(dashboard, "render", wraps=dashboard.render)

    # A, Enter (row 0's actions), Enter ("Use a stored login…"), Enter (the one parked login)
    assert _drive_run(mocker, [b"A", _ENTER, _ENTER, _ENTER], [_alpha(tmp_path)]) == 0

    use = [p for p in _rendered(render, dashboard.Picker) if p.purpose == "acct-use"]
    assert use, "the stored-login picker was never drawn"
    assert use[0].title == "Use which login?"
    assert [(e.label, e.value) for e in use[0].entries] == [("b@x.io~2", "b@x.io~2")]
    assert run.call_args_list == [
        mocker.call(_ACCOUNT_LS, cwd=tmp_path),
        mocker.call(["account", "use", "b@x.io~2", "-a", "claude", "-g", "team"], cwd=tmp_path),
    ]
    assert render.call_args_list[-1].kwargs["overlay"] is None


@pytest.mark.parametrize(("downs", "group"), [(0, "spare"), (1, "team")])
def test_accounts_use_a_parked_login_in_a_chosen_group(mocker, tmp_path, downs, group):
    run = _fake_accounts_cli(mocker)
    render = mocker.patch.object(dashboard, "render", wraps=dashboard.render)

    # A, Down (the parked row), Enter, Enter ("Use in a group…"), [Down], Enter
    keys = [b"A", b"j", _ENTER, _ENTER, *[b"j"] * downs, _ENTER]
    assert _drive_run(mocker, keys, [_alpha(tmp_path)]) == 0

    use_in = [p for p in _rendered(render, dashboard.Picker) if p.purpose == "acct-use-in"]
    assert [e.value for e in use_in[0].entries] == ["spare", "team"]
    assert run.call_args_list == [
        mocker.call(_ACCOUNT_LS, cwd=tmp_path),
        mocker.call(["account", "use", "b@x.io~2", "-a", "claude", "-g", group], cwd=tmp_path),
    ]
    assert render.call_args_list[-1].kwargs["overlay"] is None


def test_accounts_panel_closes_when_its_repo_vanishes(mocker, tmp_path):
    groups = [_alpha(tmp_path)]
    run = _fake_accounts_cli(mocker)
    render = mocker.patch.object(dashboard, "render", wraps=dashboard.render)
    script = iter([b"A"])

    def read(_fd, _n):
        item = next(script, None)
        if item is not None:
            return item
        if groups:
            groups.clear()  # the repo drops out of the registry
            return b"j"
        return b"\x03"

    assert _drive_run_with_reader(mocker, read, groups) == 0

    assert run.call_count == 1
    last = render.call_args_list[-1].kwargs
    assert last["overlay"] is None
    assert last["notice"] == "'alpha' is gone — accounts closed"


def test_accounts_key_is_documented_in_help():
    assert dashboard.parse_key(b"A") == "accounts"
    out = _render_text(dashboard._render_help())
    line = next(ln for ln in out.splitlines() if "credential groups and stored logins" in ln)
    assert line.split()[1] == "A"
    assert "Accounts panel: Enter acts on a login or group, n creates a group." in out


# A, Down (the parked row b@x.io~2), Enter, Down ("Delete this login…"), Enter
_OPEN_DELETE_CONFIRM = [b"A", b"j", _ENTER, b"j", _ENTER]
# A, Down x2 (the empty "spare" group), Enter, Down ("Remove this group"), Enter
_OPEN_GROUP_RM_CONFIRM = [b"A", b"j", b"j", _ENTER, b"j", _ENTER]


@pytest.mark.parametrize(
    ("keys", "title", "argv"),
    [
        (
            _OPEN_DELETE_CONFIRM,
            "Really delete login b@x.io~2?",
            ["account", "rm", "b@x.io~2", "-a", "claude", "--yes"],
        ),
        (
            _OPEN_GROUP_RM_CONFIRM,
            "Really remove group spare?",
            ["account", "group", "rm", "spare", "--yes"],
        ),
    ],
    ids=["delete-login", "remove-group"],
)
def test_accounts_confirmation_yes_runs_the_removal_and_closes(mocker, tmp_path, keys, title, argv):
    run = _fake_accounts_cli(mocker)
    render = mocker.patch.object(dashboard, "render", wraps=dashboard.render)

    assert _drive_run(mocker, [*keys, b"j", _ENTER], [_alpha(tmp_path)]) == 0

    confirm = next(p for p in _rendered(render, dashboard.Picker) if p.purpose == "acct-confirm")
    assert confirm.title == title
    assert [(e.label, e.value) for e in confirm.entries] == [
        ("No", "no"),
        ("Yes, delete", "yes"),
    ]
    assert confirm.index == 0  # "No" is where the cursor starts
    assert run.call_args_list == [
        mocker.call(_ACCOUNT_LS, cwd=tmp_path),
        mocker.call(argv, cwd=tmp_path),
    ]
    assert render.call_args_list[-1].kwargs["overlay"] is None


@pytest.mark.parametrize(
    "keys", [_OPEN_DELETE_CONFIRM, _OPEN_GROUP_RM_CONFIRM], ids=["delete-login", "remove-group"]
)
def test_accounts_confirmation_stray_enter_removes_nothing(mocker, tmp_path, keys):
    run = _fake_accounts_cli(mocker)
    render = mocker.patch.object(dashboard, "render", wraps=dashboard.render)

    assert _drive_run(mocker, [*keys, _ENTER], [_alpha(tmp_path)]) == 0

    assert run.call_args_list == [mocker.call(_ACCOUNT_LS, cwd=tmp_path)]
    calls = render.call_args_list
    confirm_at = max(
        i
        for i, c in enumerate(calls)
        if isinstance(c.kwargs["overlay"], dashboard.Picker)
        and c.kwargs["overlay"].purpose == "acct-confirm"
    )
    back = calls[confirm_at + 1].kwargs["overlay"]
    assert isinstance(back, dashboard.da.AccountsState)
    assert back is calls[confirm_at].kwargs["overlay"].back  # the same panel, not reloaded


def test_accounts_new_group_prompt_creates_the_typed_group_and_closes(mocker, tmp_path):
    run = _fake_accounts_cli(mocker, change=dashboard.da.CliResult(True, "Created group spare2."))
    render = mocker.patch.object(dashboard, "render", wraps=dashboard.render)

    assert _drive_run(mocker, [b"A", b"n", *_keys("spare2"), _ENTER], [_alpha(tmp_path)]) == 0

    prompt = _rendered(render, dashboard.TextPrompt)[0]
    assert (prompt.purpose, prompt.title, prompt.label, prompt.target) == (
        "acct-group-new",
        "New credential group",
        "Group name",
        "alpha",
    )
    assert run.call_args_list == [
        mocker.call(_ACCOUNT_LS, cwd=tmp_path),
        mocker.call(["account", "group", "create", "spare2"], cwd=tmp_path),
    ]
    last = render.call_args_list[-1].kwargs
    assert last["overlay"] is None
    assert last["notice"] == "Created group spare2."


def test_accounts_new_group_prompt_rejects_a_blank_name_inline(mocker, tmp_path):
    run = _fake_accounts_cli(mocker)
    render = mocker.patch.object(dashboard, "render", wraps=dashboard.render)

    assert _drive_run(mocker, [b"A", b"n", b" ", _ENTER, b"z"], [_alpha(tmp_path)]) == 0

    assert run.call_count == 1  # the listing only
    prompts = _rendered(render, dashboard.TextPrompt)
    assert prompts[-1].error is None and prompts[-1].text == " z"  # still editing after
    assert any(p.error == "Group name cannot be empty" for p in prompts)


# How to reach each question the panel can ask, and what it is.
_ACCOUNT_QUESTIONS = pytest.mark.parametrize(
    ("keys", "purpose"),
    [
        ([b"A", _ENTER], "acct-action"),
        ([b"A", _ENTER, _ENTER], "acct-use"),
        ([b"A", b"j", _ENTER, _ENTER], "acct-use-in"),
        (_OPEN_DELETE_CONFIRM, "acct-confirm"),
        (_OPEN_GROUP_RM_CONFIRM, "acct-confirm"),
        ([b"A", b"n", *_keys("x")], "acct-group-new"),
    ],
    ids=["actions", "use", "use-in", "confirm-delete", "confirm-group-rm", "name-prompt"],
)


@_ACCOUNT_QUESTIONS
@pytest.mark.parametrize("cancel", [_ESC, b"\x03", "SIGINT"], ids=["esc", "ctrl-c", "sigint"])
def test_accounts_cancel_at_every_question_returns_to_the_panel(
    mocker, tmp_path, keys, purpose, cancel
):
    run = _fake_accounts_cli(mocker)
    child = mocker.patch.object(dashboard.subprocess, "run")
    render = mocker.patch.object(dashboard, "render", wraps=dashboard.render)
    # after the cancel, `j` must move the panel's cursor: still open, still live
    script = iter([*keys, cancel, b"j"])

    assert _drive_run_with_reader(mocker, _sigint_or(script), [_alpha(tmp_path)]) == 0

    calls = render.call_args_list
    asked_at = max(
        i
        for i, c in enumerate(calls)
        if isinstance(c.kwargs["overlay"], (dashboard.Picker, dashboard.TextPrompt))
    )
    question = calls[asked_at].kwargs["overlay"]
    assert question.purpose == purpose
    after = calls[asked_at + 1].kwargs["overlay"]
    assert after is question.back
    assert isinstance(after, dashboard.da.AccountsState)
    assert calls[asked_at + 1].kwargs["notice"] == "Cancelled"  # Esc says so, like Ctrl-C
    moved = calls[asked_at + 2].kwargs["overlay"]
    assert isinstance(moved, dashboard.da.AccountsState)
    assert moved.index == min(after.index + 1, len(after.rows) - 1)
    assert run.call_args_list == [mocker.call(_ACCOUNT_LS, cwd=tmp_path)]
    child.assert_not_called()


@pytest.mark.parametrize(
    "keys",
    [
        [b"A", _ENTER],
        [b"A", _ENTER, _ENTER],
        [b"A", b"j", _ENTER, _ENTER],
        _OPEN_DELETE_CONFIRM,
    ],
    ids=["actions", "use", "use-in", "confirm-delete"],
)
def test_q_at_an_accounts_picker_steps_back_one_level_like_esc(mocker, tmp_path, keys):
    """`q` in a nested picker must not close the whole Accounts panel."""
    run = _fake_accounts_cli(mocker)
    render = mocker.patch.object(dashboard, "render", wraps=dashboard.render)

    assert _drive_run(mocker, [*keys, b"q"], [_alpha(tmp_path)]) == 0

    calls = render.call_args_list
    asked_at = max(
        i for i, c in enumerate(calls) if isinstance(c.kwargs["overlay"], dashboard.Picker)
    )
    after = calls[asked_at + 1].kwargs
    assert after["overlay"] is calls[asked_at].kwargs["overlay"].back
    assert isinstance(after["overlay"], dashboard.da.AccountsState)
    assert after["notice"] == "Cancelled"
    assert run.call_args_list == [mocker.call(_ACCOUNT_LS, cwd=tmp_path)]


def test_esc_at_a_top_level_picker_closes_it_with_a_cancelled_notice(mocker, tmp_path):
    group = dashboard.RepoGroup("alpha", str(tmp_path), None, [])
    _fake_account_cli(mocker, listing=_groups_listing("[]"))
    render = mocker.patch.object(dashboard, "render", wraps=dashboard.render)

    assert _drive_run(mocker, [*_OPEN_REPO_GROUP_PICKER, _ESC], [group]) == 0

    last = render.call_args_list[-1].kwargs
    assert (last["overlay"], last["notice"]) == (None, "Cancelled")


@pytest.mark.parametrize("cancel", [_ESC, b"q"], ids=["esc", "q"])
def test_accounts_esc_or_q_on_the_panel_closes_it(mocker, tmp_path, cancel):
    run = _fake_accounts_cli(mocker)
    render = mocker.patch.object(dashboard, "render", wraps=dashboard.render)

    # j after closing moves the table cursor: the dashboard is back in the main view
    assert _drive_run(mocker, [b"A", cancel, b"j"], [_alpha(tmp_path)]) == 0

    last = render.call_args_list[-1]
    assert last.kwargs["overlay"] is None
    assert last.args[1] == dashboard.Row("container", "alpha-x")
    assert run.call_count == 1


@pytest.mark.parametrize("ctrl_c", [b"\x03", "SIGINT"], ids=["byte", "keyboard-interrupt"])
def test_ctrl_c_on_the_bare_accounts_panel_quits(mocker, tmp_path, ctrl_c):
    """A documented choice: the panel has no text input, so Ctrl-C keeps its
    generic meaning there (quit), unlike the questions opened from it."""
    _fake_accounts_cli(mocker)
    reads = []
    script = iter([b"A", ctrl_c])

    def read(fd, n):
        reads.append(n)
        # EOF after the script (see above): fail on the count, never hang.
        return _sigint_or(script)(fd, n) if len(reads) <= 2 else b""

    assert _drive_run_with_reader(mocker, read, [_alpha(tmp_path)]) == 0
    assert len(reads) == 2  # the Ctrl-C at the panel ended the loop


@pytest.mark.parametrize("when", ["frame-before-enter", "same-read-as-enter"])
@pytest.mark.parametrize(
    "keys",
    [
        [b"A", _ENTER, b"j"],  # the actions picker, on "Park the live login"
        [b"A", b"n", *_keys("spare2")],  # the new-group prompt, answer typed
    ],
    ids=["park-picker", "name-prompt"],
)
def test_accounts_repo_vanishing_while_a_question_is_open_runs_nothing(
    mocker, tmp_path, keys, when
):
    group = _alpha(tmp_path)
    groups = [group]
    run = _fake_accounts_cli(mocker)
    child = mocker.patch.object(dashboard.subprocess, "run")
    render = mocker.patch.object(dashboard, "render", wraps=dashboard.render)
    script = iter([*keys, "vanish", _ENTER])

    def read(_fd, _n):
        item = next(script, b"\x03")
        if item != "vanish":
            return item
        if when == "frame-before-enter":
            groups.clear()  # the next frame no longer lists the repo
            return b"\x1b[C"  # an inert key (right arrow)
        # this read already holds the Enter: only the submit's re-resolve can catch it
        group.repo_root = None
        return _ENTER

    assert _drive_run_with_reader(mocker, read, groups) == 0

    assert run.call_args_list == [mocker.call(_ACCOUNT_LS, cwd=tmp_path)]
    child.assert_not_called()
    notices = " ".join(str(c.kwargs["notice"]) for c in render.call_args_list)
    assert "'alpha' is gone" in notices


# --- _run_cli_foreground: dashboard-built argv in the real terminal -----------


def _target(tmp_path: Path) -> dashboard.RepoTarget:
    return dashboard.RepoTarget(tmp_path, tmp_path / "c.yaml")


def test_run_cli_foreground_inserts_config_before_the_separator_and_pauses(mocker, tmp_path):
    run = mocker.patch.object(dashboard.subprocess, "run")
    run.return_value.returncode = 0
    wait = mocker.patch.object(dashboard, "_wait_for_return")

    rc = dashboard._run_cli_foreground(
        _target(tmp_path), ["snapshot", "create", "--", "alpha-x", "t"], style="output"
    )

    assert rc == 0
    run.assert_called_once_with(
        [
            "jailbee",
            "snapshot",
            "create",
            "--config",
            str(tmp_path / "c.yaml"),
            "--",
            "alpha-x",
            "t",
        ],
        check=False,
        cwd=tmp_path,
    )
    wait.assert_called_once()


def test_run_cli_foreground_over_ssh_sends_no_config_flag(mocker, tmp_path):
    from jailbee.config.models_remote import RemoteSSHConfig

    run = mocker.patch.object(dashboard.subprocess, "run")
    run.return_value.returncode = 0
    mocker.patch.object(dashboard, "_wait_for_return")

    dashboard._run_cli_foreground(
        _target(tmp_path),
        ["disk-usage"],
        style="output",
        remote=True,
        over_ssh=True,
        ssh_policy=RemoteSSHConfig(),
    )

    run.assert_called_once_with(["jailbee", "disk-usage"], check=False, cwd=tmp_path)


def test_run_cli_foreground_refuses_before_spawning(mocker, tmp_path):
    from jailbee.config.models_remote import RemoteSSHConfig
    from jailbee.remote_ssh.router import RouteError

    run = mocker.patch.object(dashboard.subprocess, "run")
    paged = mocker.patch.object(dashboard, "_run_paged")

    with pytest.raises(RouteError):
        dashboard._run_cli_foreground(
            _target(tmp_path),
            ["apply"],
            style="paged",
            remote=True,
            over_ssh=True,
            ssh_policy=RemoteSSHConfig(),
        )

    run.assert_not_called()
    paged.assert_not_called()


def test_run_cli_foreground_pages_locally(mocker, tmp_path):
    mocker.patch.object(dashboard, "pager_argv", return_value=["less", "-R"])
    paged = mocker.patch.object(dashboard, "_run_paged", return_value=3)
    run = mocker.patch.object(dashboard.subprocess, "run")

    rc = dashboard._run_cli_foreground(_target(tmp_path), ["doctor"], style="paged")

    assert rc == 3
    paged.assert_called_once_with(
        ["jailbee", "doctor", "--config", str(tmp_path / "c.yaml")], ["less", "-R"], tmp_path
    )
    run.assert_not_called()


def test_run_cli_foreground_never_pages_a_remote_session(mocker, tmp_path):
    from jailbee.config.models_remote import RemoteSSHConfig

    mocker.patch.object(dashboard, "pager_argv", return_value=["less", "-R"])
    paged = mocker.patch.object(dashboard, "_run_paged")
    run = mocker.patch.object(dashboard.subprocess, "run")
    run.return_value.returncode = 0
    wait = mocker.patch.object(dashboard, "_wait_for_return")

    dashboard._run_cli_foreground(
        _target(tmp_path),
        ["doctor"],
        style="paged",
        remote=True,
        over_ssh=True,
        ssh_policy=RemoteSSHConfig(),
    )

    paged.assert_not_called()
    run.assert_called_once_with(["jailbee", "doctor"], check=False, cwd=tmp_path)
    wait.assert_called_once()


def test_run_cli_foreground_falls_back_to_a_pause_when_the_pager_fails(mocker, tmp_path):
    mocker.patch.object(dashboard, "pager_argv", return_value=["less", "-R"])
    mocker.patch.object(
        dashboard, "_run_paged", side_effect=dashboard._PagerUnavailableError("gone")
    )
    run = mocker.patch.object(dashboard.subprocess, "run")
    run.return_value.returncode = 0
    wait = mocker.patch.object(dashboard, "_wait_for_return")

    assert dashboard._run_cli_foreground(_target(tmp_path), ["doctor"], style="paged") == 0
    run.assert_called_once()
    wait.assert_called_once()


def test_run_cli_foreground_plain_does_not_pause(mocker, tmp_path):
    run = mocker.patch.object(dashboard.subprocess, "run")
    run.return_value.returncode = 0
    wait = mocker.patch.object(dashboard, "_wait_for_return")

    dashboard._run_cli_foreground(_target(tmp_path), ["disk-usage"], style="plain")

    wait.assert_not_called()


@pytest.mark.parametrize(("remote", "over_ssh"), [(False, True), (True, False)])
def test_run_cli_foreground_either_remote_flag_alone_forbids_the_pager(
    mocker, tmp_path, remote, over_ssh
):
    from jailbee.config.models_remote import RemoteSSHConfig

    pager = mocker.patch.object(dashboard, "pager_argv", return_value=["less", "-R"])
    paged = mocker.patch.object(dashboard, "_run_paged")
    run = mocker.patch.object(dashboard.subprocess, "run")
    run.return_value.returncode = 0
    wait = mocker.patch.object(dashboard, "_wait_for_return")

    dashboard._run_cli_foreground(
        _target(tmp_path),
        ["doctor"],
        style="paged",
        remote=remote,
        over_ssh=over_ssh,
        ssh_policy=RemoteSSHConfig(),
    )

    pager.assert_not_called()
    paged.assert_not_called()
    run.assert_called_once()
    wait.assert_called_once()


def test_repo_menu_diagnostics_submenu_then_prune_before_fold(tmp_path):
    menu = dashboard.open_repo_menu([_cfg_group(tmp_path)], "alpha", frozenset())
    assert menu is not None
    labels = [i.label if isinstance(i, dashboard.MenuGroup) else i[0] for i in menu.actions]
    at = labels.index("Diagnostics →")
    assert labels[at - 1] == "Apply config…"
    assert labels[at + 1 : at + 3] == ["Prune stale containers…", "Fold"]
    diagnostics = menu.actions[at]
    assert isinstance(diagnostics, dashboard.MenuGroup)
    assert diagnostics.actions == (("Doctor", "doctor"), ("Disk usage", "disk-usage"))


@pytest.mark.parametrize(
    ("over_ssh", "policy_kwargs", "expected"),
    [
        (False, None, {"doctor", "disk-usage", "prune"}),
        (False, {"commands": {"mode": "disabled"}}, {"doctor", "disk-usage", "prune"}),
        (True, {}, {"doctor", "disk-usage", "prune"}),
        (True, {"commands": {"mode": "allowlist", "allow": ["doctor"]}}, {"doctor"}),
        (True, {"commands": {"mode": "allowlist", "allow": ["prune"]}}, {"prune"}),
        (True, {"commands": {"mode": "allowlist", "allow": ["shell"]}}, set()),
        (True, {"excluded_repos": ["other"]}, {"prune"}),
    ],
    ids=[
        "local",
        "local-ignores-policy",
        "ssh-default",
        "ssh-allowlist-doctor",
        "ssh-allowlist-prune",
        "ssh-allowlist-without",
        "ssh-excluded-repos",
    ],
)
def test_repo_menu_diagnostics_and_prune_follow_the_ssh_policy(
    tmp_path, over_ssh, policy_kwargs, expected
):
    menu = dashboard.open_repo_menu(
        [_cfg_group(tmp_path)],
        "alpha",
        frozenset(),
        ssh_policy=_ssh_policy(policy_kwargs),
        over_ssh=over_ssh,
    )
    assert _repo_menu_verbs(menu) & {"doctor", "disk-usage", "prune"} == expected
    assert menu is not None
    labels = [i.label for i in menu.actions if isinstance(i, dashboard.MenuGroup)]
    # the submenu is dropped, not left empty, when both leaves are refused
    assert ("Diagnostics →" in labels) is bool(expected & {"doctor", "disk-usage"})


def test_repo_doctor_is_paged_locally(mocker, tmp_path):
    group = _cfg_group(tmp_path)
    mocker.patch.object(dashboard, "pager_argv", return_value=["less", "-R"])
    paged = mocker.patch.object(dashboard, "_run_paged", return_value=0)
    child = mocker.patch.object(dashboard.subprocess, "run")

    assert _drive_run(mocker, _repo_menu_keys(group, "doctor"), [group]) == 0

    paged.assert_called_once_with(
        ["jailbee", "doctor", "--config", str(group.config_path)], ["less", "-R"], tmp_path
    )
    child.assert_not_called()


def test_repo_doctor_over_ssh_pauses_instead_of_paging_and_sends_no_config(mocker, tmp_path):
    from jailbee.config.models_remote import RemoteSSHConfig

    group = _cfg_group(tmp_path)
    policy = RemoteSSHConfig()
    mocker.patch.object(dashboard, "pager_argv", return_value=["less", "-R"])
    paged = mocker.patch.object(dashboard, "_run_paged")
    child = mocker.patch.object(dashboard.subprocess, "run")
    child.return_value.returncode = 0
    wait = mocker.patch.object(dashboard, "_wait_for_return")

    keys = _repo_menu_keys(group, "doctor", ssh_policy=policy, over_ssh=True)
    assert _drive_run(mocker, keys, [group], remote=True, over_ssh=True, ssh_policy=policy) == 0

    paged.assert_not_called()
    child.assert_called_once_with(["jailbee", "doctor"], check=False, cwd=tmp_path)
    wait.assert_called_once()


@pytest.mark.parametrize(
    ("verb", "argv"),
    [("disk-usage", ["disk-usage"]), ("prune", ["prune"])],
    ids=["disk-usage", "prune"],
)
@pytest.mark.parametrize("over_ssh", [False, True], ids=["local", "ssh-default"])
def test_repo_disk_usage_and_prune_run_in_the_terminal_with_a_pause(
    mocker, tmp_path, verb, argv, over_ssh
):
    from jailbee.config.models_remote import RemoteSSHConfig

    group = _cfg_group(tmp_path)
    policy = RemoteSSHConfig() if over_ssh else None
    child = mocker.patch.object(dashboard.subprocess, "run")
    child.return_value.returncode = 0
    wait = mocker.patch.object(dashboard, "_wait_for_return")

    keys = _repo_menu_keys(group, verb, ssh_policy=policy, over_ssh=over_ssh)
    assert (
        _drive_run(mocker, keys, [group], remote=over_ssh, over_ssh=over_ssh, ssh_policy=policy)
        == 0
    )

    flags = [] if over_ssh else ["--config", str(group.config_path)]
    child.assert_called_once_with(["jailbee", *argv, *flags], check=False, cwd=tmp_path)
    assert "--yes-to-all" not in child.call_args.args[0]
    wait.assert_called_once()


def test_repo_doctor_failure_is_a_notice(mocker, tmp_path):
    group = _cfg_group(tmp_path)
    mocker.patch.object(dashboard, "pager_argv", return_value=None)
    child = mocker.patch.object(dashboard.subprocess, "run")
    child.return_value.returncode = 1
    mocker.patch.object(dashboard, "_wait_for_return")
    render = mocker.patch.object(dashboard, "render", wraps=dashboard.render)

    _drive_run(mocker, _repo_menu_keys(group, "doctor"), [group])

    assert "'jailbee doctor' exited 1" in _notices(render)


# --- Terminal-only container entries (autostart, snapshots, mounts) ---------


def _autostart_ci(phase: str = "autostart") -> ContainerInfo:
    """A container whose job row is an autostart run; os.getpid() keeps the worker alive."""
    return dataclasses.replace(
        _ci("alpha-x", "alpha", job_phase=phase, job_pid=os.getpid()), job_kind="autostart"
    )


def _container_menu_keys(group: dashboard.RepoGroup, verb: str, **menu_kwargs) -> list[bytes]:
    """Keys that choose top-level container-menu leaf ``verb`` for the first container.

    ``menu_kwargs`` (``remote``/``over_ssh``/``ssh_policy``) must match the ``run()`` call.
    """
    menu = dashboard.open_menu([group], group.containers[0].name, **menu_kwargs)
    assert menu is not None
    entries = list(dashboard._menu_entries(menu))
    at = next(
        i
        for i, entry in enumerate(entries)
        if not isinstance(entry, dashboard.MenuGroup) and entry[1] == verb
    )
    return [b"j", _ENTER, *[b"j"] * at, _ENTER]


def test_container_menu_places_autostart_after_the_job_log_and_snapshots_before_the_group(
    tmp_path,
):
    menu = dashboard.open_menu([_cfg_group(tmp_path, (_autostart_ci(),))], "alpha-x")
    assert menu is not None
    verbs = [verb for _label, verb in menu.actions]
    at = verbs.index("job log --follow")
    assert verbs[at + 1 : at + 3] == ["autostart-status", "autostart-cancel"]
    assert verbs[verbs.index("snapshots") + 1] == "credential-group"
    # Credential group… still sits directly above the Network → group
    assert verbs[verbs.index("credential-group") + 1].startswith("net ")


def test_terminal_only_verbs_never_reach_the_shared_action_list(tmp_path):
    group = _cfg_group(tmp_path, (_autostart_ci(),))
    group.optional_mounts = ("aws",)
    shared = {verb for _label, verb in dashboard.actions_for_container([group], "alpha-x")}
    assert not shared & dashboard.TERMINAL_MENU_VERBS


@pytest.mark.parametrize(
    ("verbs", "expected"),
    [
        (["tmux", "job log", "net loose", "destroy"], ["tmux", "job log", "X", "net loose"]),
        (["tmux", "net loose", "destroy"], ["tmux", "X", "net loose"]),
        (["tmux", "restart", "destroy"], ["tmux", "X", "restart"]),
    ],
    ids=["after-the-job-entry", "no-job-entry-before-network", "no-network-before-lifecycle"],
)
def test_insert_after_job_falls_back_to_before_network(verbs, expected):
    actions = [(verb.title(), verb) for verb in verbs]
    placed = [verb for _label, verb in dashboard._insert_after_job(actions, [("X", "X")])]
    assert placed[: len(expected)] == expected


@pytest.mark.parametrize(
    ("over_ssh", "policy_kwargs", "expected"),
    [
        (False, None, {"autostart-status", "autostart-cancel"}),
        (False, {"commands": {"mode": "disabled"}}, {"autostart-status", "autostart-cancel"}),
        (True, {}, {"autostart-status", "autostart-cancel"}),
        (
            True,
            {"commands": {"mode": "allowlist", "allow": ["shell", "autostart status"]}},
            {"autostart-status"},
        ),
        (True, {"commands": {"mode": "allowlist", "allow": ["shell"]}}, set()),
        (True, {"excluded_repos": ["other"]}, set()),
    ],
    ids=[
        "local",
        "local-ignores-policy",
        "ssh-default",
        "ssh-allowlist-status",
        "ssh-allowlist-without",
        "ssh-excluded-repos",
    ],
)
def test_container_menu_autostart_entries_follow_the_ssh_policy(
    tmp_path, over_ssh, policy_kwargs, expected
):
    menu = dashboard.open_menu(
        [_cfg_group(tmp_path, (_autostart_ci(),))],
        "alpha-x",
        remote=over_ssh,
        over_ssh=over_ssh,
        ssh_policy=_ssh_policy(policy_kwargs),
    )
    assert menu is not None
    assert {verb for _label, verb in menu.actions} & {"autostart-status", "autostart-cancel"} == (
        expected
    )


@pytest.mark.parametrize("over_ssh", [False, True], ids=["local", "ssh-default"])
def test_autostart_status_runs_in_the_terminal(mocker, tmp_path, over_ssh):
    from jailbee.config.models_remote import RemoteSSHConfig

    group = _cfg_group(tmp_path, (_autostart_ci(),))
    policy = RemoteSSHConfig() if over_ssh else None
    child = mocker.patch.object(dashboard.subprocess, "run")
    child.return_value.returncode = 0
    wait = mocker.patch.object(dashboard, "_wait_for_return")
    kwargs = {"remote": over_ssh, "over_ssh": over_ssh, "ssh_policy": policy}

    keys = _container_menu_keys(group, "autostart-status", **kwargs)
    assert _drive_run(mocker, keys, [group], **kwargs) == 0

    flags = [] if over_ssh else ["--config", str(group.config_path)]
    child.assert_called_once_with(
        ["jailbee", "autostart", "status", "alpha-x", *flags], check=False, cwd=tmp_path
    )
    wait.assert_called_once()


def test_cancel_autostart_asks_first_and_no_runs_nothing(mocker, tmp_path):
    group = _cfg_group(tmp_path, (_autostart_ci(),))
    child = mocker.patch.object(dashboard.subprocess, "run")
    render = mocker.patch.object(dashboard, "render", wraps=dashboard.render)

    keys = [*_container_menu_keys(group, "autostart-cancel"), _ENTER]  # Enter on "No"
    assert _drive_run(mocker, keys, [group]) == 0

    assert [e.value for e in _rendered(render, dashboard.Picker)[0].entries] == ["no", "yes"]
    child.assert_not_called()
    assert "Cancelled" in _notices(render)


@pytest.mark.parametrize("over_ssh", [False, True], ids=["local", "ssh-default"])
def test_cancel_autostart_yes_runs_the_cancel(mocker, tmp_path, over_ssh):
    from jailbee.config.models_remote import RemoteSSHConfig

    group = _cfg_group(tmp_path, (_autostart_ci(),))
    policy = RemoteSSHConfig() if over_ssh else None
    child = mocker.patch.object(dashboard.subprocess, "run")
    child.return_value.returncode = 0
    mocker.patch.object(dashboard, "_wait_for_return")
    kwargs = {"remote": over_ssh, "over_ssh": over_ssh, "ssh_policy": policy}

    keys = [*_container_menu_keys(group, "autostart-cancel", **kwargs), b"j", _ENTER]
    assert _drive_run(mocker, keys, [group], **kwargs) == 0

    flags = [] if over_ssh else ["--config", str(group.config_path)]
    child.assert_called_once_with(
        ["jailbee", "autostart", "cancel", "alpha-x", *flags], check=False, cwd=tmp_path
    )


# --- Vanish, inert and stale-policy protection of the terminal-only entries --


def _drive_with_vanish(mocker, keys, groups, vanish, *, when: str) -> int:
    """Run the loop; ``vanish()`` fires after ``keys``: a frame before Enter, or on its read."""
    script = iter([*keys, "vanish", *([_ENTER] if when == "frame-before-enter" else [])])

    def read(_fd, _n):
        item = next(script, b"\x03")
        if item == "vanish":
            vanish()
            return b"z" if when == "frame-before-enter" else _ENTER
        return item

    return _drive_run_with_reader(mocker, read, groups)


_VANISH_WHEN = pytest.mark.parametrize("when", ["frame-before-enter", "same-read-as-enter"])


@_VANISH_WHEN
def test_container_vanishing_while_the_autostart_cancel_picker_is_open_runs_nothing(
    mocker, tmp_path, when
):
    group = _cfg_group(tmp_path, (_autostart_ci(),))
    child = mocker.patch.object(dashboard.subprocess, "run")
    render = mocker.patch.object(dashboard, "render", wraps=dashboard.render)

    # ``j`` moves the cursor from "No" to "Yes"; the container then disappears
    keys = [*_container_menu_keys(group, "autostart-cancel"), b"j"]
    assert _drive_with_vanish(mocker, keys, [group], group.containers.clear, when=when) == 0

    child.assert_not_called()
    assert "'alpha-x' is gone" in " ".join(str(n) for n in _notices(render))


@pytest.mark.parametrize("when", ["frame-before-enter", "same-read-as-enter"])
def test_repo_vanishing_while_the_apply_picker_is_open_runs_nothing(mocker, tmp_path, when):
    group = _cfg_group(tmp_path)
    groups = [group]
    child = mocker.patch.object(dashboard.subprocess, "run")
    render = mocker.patch.object(dashboard, "render", wraps=dashboard.render)
    gone = False
    real_target_group = dashboard.target_group

    def target_group(seen, target, kind):
        return None if gone and kind == "repo" else real_target_group(seen, target, kind)

    mocker.patch.object(dashboard, "target_group", side_effect=target_group)

    def vanish():
        nonlocal gone
        if when == "frame-before-enter":
            groups.clear()  # the next frame's loop-top guard closes the picker
        else:
            gone = True  # the frame still lists it; only the submit's re-resolve can notice

    assert (
        _drive_with_vanish(mocker, _repo_menu_keys(group, "apply"), groups, vanish, when=when) == 0
    )

    child.assert_not_called()
    assert "'alpha' is gone" in " ".join(str(n) for n in _notices(render))


def test_stale_menu_refused_by_the_policy_at_submit_spawns_nothing(mocker, tmp_path):
    group = _cfg_group(tmp_path, (_autostart_ci(),))
    child = mocker.patch.object(dashboard.subprocess, "run")
    render = mocker.patch.object(dashboard, "render", wraps=dashboard.render)
    keys = _container_menu_keys(group, "autostart-status")  # built while the menu offers it
    real_check = dashboard.check_dashboard_command

    def refuse(argv, policy, *, over_ssh):
        if argv[:2] == ["autostart", "status"]:
            raise dashboard.RouteError("autostart status is not permitted")
        return real_check(argv, policy, over_ssh=over_ssh)

    mocker.patch.object(dashboard, "check_dashboard_command", side_effect=refuse)

    assert _drive_run(mocker, keys, [group]) == 0

    child.assert_not_called()
    assert "autostart status is not permitted" in _notices(render)


# --- Snapshots…: listing and create -----------------------------------------

_SNAPS_JSON = '[{"name": "before-upgrade", "created": "2026-09-29T10:00:00.5Z"}]'
_SNAPSHOT_LS = ["snapshot", "ls", "alpha-x", "-o", "json", "--fields", "name,created"]


def _fake_snapshot_ls(mocker, result=None):
    """Patch the quiet runner the snapshot listing goes through."""
    return mocker.patch.object(
        dashboard.da,
        "run_cli_quiet",
        return_value=result or dashboard.da.CliResult(True, "done", _SNAPS_JSON),
    )


@pytest.mark.parametrize("over_ssh", [False, True], ids=["local", "ssh-default"])
def test_snapshots_lists_quietly_and_offers_create_above_the_snapshots(mocker, tmp_path, over_ssh):
    from jailbee.config.models_remote import RemoteSSHConfig

    group = _cfg_group(tmp_path, (_ci("alpha-x", "alpha"),))
    policy = RemoteSSHConfig() if over_ssh else None
    listing = _fake_snapshot_ls(mocker)
    child = mocker.patch.object(dashboard.subprocess, "run")
    render = mocker.patch.object(dashboard, "render", wraps=dashboard.render)
    kwargs = {"remote": over_ssh, "over_ssh": over_ssh, "ssh_policy": policy}

    assert (
        _drive_run(mocker, _container_menu_keys(group, "snapshots", **kwargs), [group], **kwargs)
        == 0
    )

    flags = [] if over_ssh else ["--config", str(group.config_path)]
    listing.assert_called_once_with([*_SNAPSHOT_LS, *flags], cwd=tmp_path)
    picker = _rendered(render, dashboard.Picker)[0]
    assert [e.value for e in picker.entries] == [
        "create:timestamp",
        "create:named",
        "snapshot:before-upgrade",
    ]
    child.assert_not_called()  # listing is quiet: the screen never blanked


@pytest.mark.parametrize("over_ssh", [False, True], ids=["local", "ssh-default"])
def test_snapshot_create_with_a_timestamp_runs_in_the_terminal(mocker, tmp_path, over_ssh):
    from jailbee.config.models_remote import RemoteSSHConfig

    group = _cfg_group(tmp_path, (_ci("alpha-x", "alpha"),))
    policy = RemoteSSHConfig() if over_ssh else None
    _fake_snapshot_ls(mocker)
    child = mocker.patch.object(dashboard.subprocess, "run")
    child.return_value.returncode = 0
    wait = mocker.patch.object(dashboard, "_wait_for_return")
    kwargs = {"remote": over_ssh, "over_ssh": over_ssh, "ssh_policy": policy}

    keys = [*_container_menu_keys(group, "snapshots", **kwargs), _ENTER]
    assert _drive_run(mocker, keys, [group], **kwargs) == 0

    flags = [] if over_ssh else ["--config", str(group.config_path)]
    child.assert_called_once_with(
        ["jailbee", "snapshot", "create", *flags, "--", "alpha-x"], check=False, cwd=tmp_path
    )
    wait.assert_called_once()


def test_snapshot_create_named_takes_an_option_like_tag_as_a_tag(mocker, tmp_path):
    group = _cfg_group(tmp_path, (_ci("alpha-x", "alpha"),))
    _fake_snapshot_ls(mocker)
    child = mocker.patch.object(dashboard.subprocess, "run")
    child.return_value.returncode = 0
    mocker.patch.object(dashboard, "_wait_for_return")

    keys = [*_container_menu_keys(group, "snapshots"), b"j", _ENTER, *_keys("--yes"), _ENTER]
    assert _drive_run(mocker, keys, [group]) == 0

    child.assert_called_once_with(
        [
            "jailbee",
            "snapshot",
            "create",
            "--config",
            str(group.config_path),
            "--",
            "alpha-x",
            "--yes",
        ],
        check=False,
        cwd=tmp_path,
    )


def _snapshot_tag_prompts(render) -> list:
    return [
        p for p in _rendered(render, dashboard.TextPrompt) if p.purpose == "container-snapshot-tag"
    ]


def test_snapshot_tag_prompt_escape_runs_nothing(mocker, tmp_path):
    group = _cfg_group(tmp_path, (_ci("alpha-x", "alpha"),))
    _fake_snapshot_ls(mocker)
    child = mocker.patch.object(dashboard.subprocess, "run")
    render = mocker.patch.object(dashboard, "render", wraps=dashboard.render)

    keys = [*_container_menu_keys(group, "snapshots"), b"j", _ENTER, *_keys("x"), _ESC]
    assert _drive_run(mocker, keys, [group]) == 0

    assert _snapshot_tag_prompts(render)
    assert "Cancelled" in _notices(render)
    child.assert_not_called()


def test_snapshot_tag_prompt_ctrl_c_cancels_only_the_prompt(mocker, tmp_path):
    group = _cfg_group(tmp_path, (_ci("alpha-x", "alpha"),))
    _fake_snapshot_ls(mocker)
    child = mocker.patch.object(dashboard.subprocess, "run")
    render = mocker.patch.object(dashboard, "render", wraps=dashboard.render)

    keys = [
        *_container_menu_keys(group, "snapshots"),
        b"j",
        _ENTER,
        *_keys("x"),
        b"\x03",
        b"h",
        _ESC,
    ]
    assert _drive_run(mocker, keys, [group]) == 0

    assert _snapshot_tag_prompts(render)
    child.assert_not_called()
    assert "Cancelled" in _notices(render)
    # the dashboard survived the Ctrl-C: the later `h` still opened help
    assert "help" in [c.kwargs.get("overlay") for c in render.call_args_list]


def test_snapshot_tag_prompt_rejects_a_blank_tag_inline(mocker, tmp_path):
    group = _cfg_group(tmp_path, (_ci("alpha-x", "alpha"),))
    _fake_snapshot_ls(mocker)
    child = mocker.patch.object(dashboard.subprocess, "run")
    render = mocker.patch.object(dashboard, "render", wraps=dashboard.render)

    keys = [*_container_menu_keys(group, "snapshots"), b"j", _ENTER, *_keys("  "), _ENTER]
    assert _drive_run(mocker, keys, [group]) == 0

    child.assert_not_called()
    assert any(p.error == "Snapshot tag cannot be empty" for p in _snapshot_tag_prompts(render))


def test_snapshot_picker_escape_runs_nothing(mocker, tmp_path):
    group = _cfg_group(tmp_path, (_ci("alpha-x", "alpha"),))
    _fake_snapshot_ls(mocker)
    child = mocker.patch.object(dashboard.subprocess, "run")
    render = mocker.patch.object(dashboard, "render", wraps=dashboard.render)

    assert _drive_run(mocker, [*_container_menu_keys(group, "snapshots"), _ESC], [group]) == 0

    assert _rendered(render, dashboard.Picker)
    child.assert_not_called()


@pytest.mark.parametrize(
    ("result", "notice"),
    [
        (dashboard.da.CliResult(False, "error: boom"), "could not list snapshots: error: boom"),
        (
            dashboard.da.CliResult(True, "done", "No snapshots"),
            "could not list snapshots: unexpected output from 'jailbee snapshot ls'",
        ),
    ],
    ids=["cli-failed", "not-json"],
)
def test_a_failed_snapshot_listing_is_a_notice(mocker, tmp_path, result, notice):
    group = _cfg_group(tmp_path, (_ci("alpha-x", "alpha"),))
    _fake_snapshot_ls(mocker, result)
    render = mocker.patch.object(dashboard, "render", wraps=dashboard.render)

    assert _drive_run(mocker, _container_menu_keys(group, "snapshots"), [group]) == 0

    assert not _rendered(render, dashboard.Picker)
    assert notice in _notices(render)


def test_snapshot_listing_is_refused_when_create_is_not_permitted_and_there_are_none(
    mocker, tmp_path
):
    from jailbee.config.models_remote import RemoteSSHConfig

    group = _cfg_group(tmp_path, (_ci("alpha-x", "alpha"),))
    policy = RemoteSSHConfig.model_validate(
        {"commands": {"mode": "allowlist", "allow": ["shell", "snapshot ls"]}}
    )
    _fake_snapshot_ls(mocker, dashboard.da.CliResult(True, "done", "[]"))
    render = mocker.patch.object(dashboard, "render", wraps=dashboard.render)
    kwargs = {"remote": True, "over_ssh": True, "ssh_policy": policy}

    assert (
        _drive_run(mocker, _container_menu_keys(group, "snapshots", **kwargs), [group], **kwargs)
        == 0
    )

    assert not _rendered(render, dashboard.Picker)
    assert "No snapshots of 'alpha-x'" in _notices(render)


def test_snapshot_picker_hides_create_when_only_the_listing_is_permitted(mocker, tmp_path):
    from jailbee.config.models_remote import RemoteSSHConfig

    group = _cfg_group(tmp_path, (_ci("alpha-x", "alpha"),))
    policy = RemoteSSHConfig.model_validate(
        {"commands": {"mode": "allowlist", "allow": ["shell", "snapshot ls"]}}
    )
    _fake_snapshot_ls(mocker)
    render = mocker.patch.object(dashboard, "render", wraps=dashboard.render)
    kwargs = {"remote": True, "over_ssh": True, "ssh_policy": policy}

    assert (
        _drive_run(mocker, _container_menu_keys(group, "snapshots", **kwargs), [group], **kwargs)
        == 0
    )

    picker = _rendered(render, dashboard.Picker)[0]
    assert [e.value for e in picker.entries] == ["snapshot:before-upgrade"]


def test_snapshot_create_permitted_over_ssh_with_an_empty_listing_still_opens_the_picker(
    mocker, tmp_path
):
    from jailbee.config.models_remote import RemoteSSHConfig

    group = _cfg_group(tmp_path, (_ci("alpha-x", "alpha"),))
    policy = RemoteSSHConfig.model_validate(
        {"commands": {"mode": "allowlist", "allow": ["shell", "snapshot ls", "snapshot create"]}}
    )
    _fake_snapshot_ls(mocker, dashboard.da.CliResult(True, "done", "[]"))
    child = mocker.patch.object(dashboard.subprocess, "run")
    child.return_value.returncode = 0
    mocker.patch.object(dashboard, "_wait_for_return")
    render = mocker.patch.object(dashboard, "render", wraps=dashboard.render)
    kwargs = {"remote": True, "over_ssh": True, "ssh_policy": policy}

    keys = [*_container_menu_keys(group, "snapshots", **kwargs), b"j", _ENTER, *_keys("--yes")]
    assert _drive_run(mocker, [*keys, _ENTER], [group], **kwargs) == 0

    picker = _rendered(render, dashboard.Picker)[0]
    assert [e.value for e in picker.entries] == ["create:timestamp", "create:named"]
    child.assert_called_once_with(
        ["jailbee", "snapshot", "create", "--", "alpha-x", "--yes"], check=False, cwd=tmp_path
    )


def test_snapshot_create_refused_by_the_policy_at_submit_spawns_nothing(mocker, tmp_path):
    group = _cfg_group(tmp_path, (_ci("alpha-x", "alpha"),))
    _fake_snapshot_ls(mocker)
    child = mocker.patch.object(dashboard.subprocess, "run")
    render = mocker.patch.object(dashboard, "render", wraps=dashboard.render)
    real_check = dashboard.check_dashboard_command

    def refuse(argv, policy, *, over_ssh):
        if argv[:2] == ["snapshot", "create"]:
            raise dashboard.RouteError("snapshot create is not permitted")
        return real_check(argv, policy, over_ssh=over_ssh)

    mocker.patch.object(dashboard, "check_dashboard_command", side_effect=refuse)

    keys = [*_container_menu_keys(group, "snapshots"), _ENTER]
    assert _drive_run(mocker, keys, [group]) == 0

    child.assert_not_called()
    assert "snapshot create is not permitted" in _notices(render)


@_VANISH_WHEN
@pytest.mark.parametrize("choice", ["timestamp", "named-tag"])
def test_container_vanishing_while_the_snapshots_picker_or_tag_prompt_is_open_runs_nothing(
    mocker, tmp_path, when, choice
):
    group = _cfg_group(tmp_path, (_ci("alpha-x", "alpha"),))
    listing = _fake_snapshot_ls(mocker)
    child = mocker.patch.object(dashboard.subprocess, "run")
    render = mocker.patch.object(dashboard, "render", wraps=dashboard.render)

    keys = _container_menu_keys(group, "snapshots")
    if choice == "named-tag":
        keys = [*keys, b"j", _ENTER, *_keys("v1")]
    assert _drive_with_vanish(mocker, keys, [group], group.containers.clear, when=when) == 0

    child.assert_not_called()
    assert listing.call_count == 1  # nothing was listed again either
    assert "'alpha-x' is gone" in " ".join(str(n) for n in _notices(render))


# --- Snapshots…: restore and delete, confirmed -------------------------------

_TO_SNAPSHOT_ROW = [b"j", b"j", _ENTER]


@pytest.mark.parametrize(
    ("action_downs", "verb"), [(0, "restore"), (1, "delete")], ids=["restore", "delete"]
)
@pytest.mark.parametrize("over_ssh", [False, True], ids=["local", "ssh-default"])
def test_snapshot_restore_and_delete_run_after_a_yes(
    mocker, tmp_path, action_downs, verb, over_ssh
):
    from jailbee.config.models_remote import RemoteSSHConfig

    group = _cfg_group(tmp_path, (_ci("alpha-x", "alpha"),))
    policy = RemoteSSHConfig() if over_ssh else None
    _fake_snapshot_ls(mocker)
    child = mocker.patch.object(dashboard.subprocess, "run")
    child.return_value.returncode = 0
    wait = mocker.patch.object(dashboard, "_wait_for_return")
    kwargs = {"remote": over_ssh, "over_ssh": over_ssh, "ssh_policy": policy}

    keys = [
        *_container_menu_keys(group, "snapshots", **kwargs),
        *_TO_SNAPSHOT_ROW,
        *[b"j"] * action_downs,
        _ENTER,
        b"j",  # "Yes, …"
        _ENTER,
    ]
    assert _drive_run(mocker, keys, [group], **kwargs) == 0

    flags = [] if over_ssh else ["--config", str(group.config_path)]
    child.assert_called_once_with(
        ["jailbee", "snapshot", verb, *flags, "--", "alpha-x", "before-upgrade"],
        check=False,
        cwd=tmp_path,
    )
    wait.assert_called_once()


@pytest.mark.parametrize("action_downs", [0, 1], ids=["restore", "delete"])
def test_snapshot_confirm_no_runs_nothing(mocker, tmp_path, action_downs):
    group = _cfg_group(tmp_path, (_ci("alpha-x", "alpha"),))
    _fake_snapshot_ls(mocker)
    child = mocker.patch.object(dashboard.subprocess, "run")
    render = mocker.patch.object(dashboard, "render", wraps=dashboard.render)

    keys = [
        *_container_menu_keys(group, "snapshots"),
        *_TO_SNAPSHOT_ROW,
        *[b"j"] * action_downs,
        _ENTER,
        _ENTER,  # a stray Enter lands on "No"
    ]
    assert _drive_run(mocker, keys, [group]) == 0

    confirm = [
        p for p in _rendered(render, dashboard.Picker) if p.purpose == "container-snapshot-confirm"
    ]
    assert confirm and confirm[0].entries[0].value == "no"
    child.assert_not_called()
    assert "Cancelled" in _notices(render)


@pytest.mark.parametrize("step", ["action", "confirm"])
@pytest.mark.parametrize("key", [_ESC, b"\x03"], ids=["esc", "ctrl-c"])
def test_snapshot_action_and_confirm_cancel_runs_nothing(mocker, tmp_path, step, key):
    group = _cfg_group(tmp_path, (_ci("alpha-x", "alpha"),))
    _fake_snapshot_ls(mocker)
    child = mocker.patch.object(dashboard.subprocess, "run")
    render = mocker.patch.object(dashboard, "render", wraps=dashboard.render)

    keys = [*_container_menu_keys(group, "snapshots"), *_TO_SNAPSHOT_ROW]
    if step == "confirm":
        keys += [b"j", _ENTER, b"j"]  # Delete, then onto "Yes"
    assert _drive_run(mocker, [*keys, key, _ENTER], [group]) == 0

    purposes = {p.purpose for p in _rendered(render, dashboard.Picker)}
    assert "container-snapshot-action" in purposes
    assert ("container-snapshot-confirm" in purposes) == (step == "confirm")
    child.assert_not_called()


def test_snapshot_changes_the_policy_refuses_are_not_offered(mocker, tmp_path):
    from jailbee.config.models_remote import RemoteSSHConfig

    group = _cfg_group(tmp_path, (_ci("alpha-x", "alpha"),))
    policy = RemoteSSHConfig.model_validate(
        {"commands": {"mode": "allowlist", "allow": ["shell", "snapshot ls", "snapshot delete"]}}
    )
    _fake_snapshot_ls(mocker)
    render = mocker.patch.object(dashboard, "render", wraps=dashboard.render)
    kwargs = {"remote": True, "over_ssh": True, "ssh_policy": policy}

    # no create entries: the listed snapshot is row 0
    keys = [*_container_menu_keys(group, "snapshots", **kwargs), _ENTER]
    assert _drive_run(mocker, keys, [group], **kwargs) == 0

    pickers = _rendered(render, dashboard.Picker)
    assert [e.value for e in pickers[0].entries] == ["snapshot:before-upgrade"]
    actions = [p for p in pickers if p.purpose == "container-snapshot-action"]
    assert [e.value for e in actions[0].entries] == ["delete"]


def test_snapshot_restore_alone_is_offered_when_delete_is_refused(mocker, tmp_path):
    from jailbee.config.models_remote import RemoteSSHConfig

    group = _cfg_group(tmp_path, (_ci("alpha-x", "alpha"),))
    policy = RemoteSSHConfig.model_validate(
        {"commands": {"mode": "allowlist", "allow": ["shell", "snapshot ls", "snapshot restore"]}}
    )
    _fake_snapshot_ls(mocker)
    render = mocker.patch.object(dashboard, "render", wraps=dashboard.render)
    kwargs = {"remote": True, "over_ssh": True, "ssh_policy": policy}

    keys = [*_container_menu_keys(group, "snapshots", **kwargs), _ENTER]
    assert _drive_run(mocker, keys, [group], **kwargs) == 0

    actions = [
        p for p in _rendered(render, dashboard.Picker) if p.purpose == "container-snapshot-action"
    ]
    assert [e.value for e in actions[0].entries] == ["restore"]


def test_a_snapshot_the_policy_permits_no_change_to_is_a_notice(mocker, tmp_path):
    from jailbee.config.models_remote import RemoteSSHConfig

    group = _cfg_group(tmp_path, (_ci("alpha-x", "alpha"),))
    policy = RemoteSSHConfig.model_validate(
        {"commands": {"mode": "allowlist", "allow": ["shell", "snapshot ls"]}}
    )
    _fake_snapshot_ls(mocker)
    child = mocker.patch.object(dashboard.subprocess, "run")
    render = mocker.patch.object(dashboard, "render", wraps=dashboard.render)
    kwargs = {"remote": True, "over_ssh": True, "ssh_policy": policy}

    keys = [*_container_menu_keys(group, "snapshots", **kwargs), _ENTER]
    assert _drive_run(mocker, keys, [group], **kwargs) == 0

    assert "No change to snapshot before-upgrade is permitted here" in _notices(render)
    assert not [p for p in _rendered(render, dashboard.Picker) if p.purpose.endswith("action")]
    child.assert_not_called()


@pytest.mark.parametrize("verb", ["restore", "delete"])
@pytest.mark.parametrize("name", ["--yes", "snapshot:x"])
def test_snapshot_names_that_look_like_options_or_sentinels_stay_positional(
    mocker, tmp_path, verb, name
):
    group = _cfg_group(tmp_path, (_ci("alpha-x", "alpha"),))
    listing = json.dumps([{"name": name, "created": "2026-09-29T10:00:00Z"}])
    _fake_snapshot_ls(mocker, dashboard.da.CliResult(True, "done", listing))
    child = mocker.patch.object(dashboard.subprocess, "run")
    child.return_value.returncode = 0
    mocker.patch.object(dashboard, "_wait_for_return")

    keys = [
        *_container_menu_keys(group, "snapshots"),
        *_TO_SNAPSHOT_ROW,
        *([b"j"] if verb == "delete" else []),
        _ENTER,
        b"j",
        _ENTER,
    ]
    assert _drive_run(mocker, keys, [group]) == 0

    child.assert_called_once_with(
        [
            "jailbee",
            "snapshot",
            verb,
            "--config",
            str(group.config_path),
            "--",
            "alpha-x",
            name,
        ],
        check=False,
        cwd=tmp_path,
    )


@pytest.mark.parametrize("verb", ["restore", "delete"])
def test_snapshot_change_refused_by_the_policy_at_submit_spawns_nothing(mocker, tmp_path, verb):
    group = _cfg_group(tmp_path, (_ci("alpha-x", "alpha"),))
    _fake_snapshot_ls(mocker)
    child = mocker.patch.object(dashboard.subprocess, "run")
    render = mocker.patch.object(dashboard, "render", wraps=dashboard.render)
    real_check = dashboard.check_dashboard_command

    def refuse(argv, policy, *, over_ssh):
        if argv[:2] == ["snapshot", verb]:
            raise dashboard.RouteError(f"snapshot {verb} is not permitted")
        return real_check(argv, policy, over_ssh=over_ssh)

    mocker.patch.object(dashboard, "check_dashboard_command", side_effect=refuse)

    keys = [
        *_container_menu_keys(group, "snapshots"),
        *_TO_SNAPSHOT_ROW,
        *([b"j"] if verb == "delete" else []),
        _ENTER,
        b"j",
        _ENTER,
    ]
    assert _drive_run(mocker, keys, [group]) == 0

    child.assert_not_called()
    assert f"snapshot {verb} is not permitted" in _notices(render)


@_VANISH_WHEN
@pytest.mark.parametrize("step", ["action", "confirm"])
def test_container_vanishing_while_a_snapshot_action_or_confirm_is_open_runs_nothing(
    mocker, tmp_path, when, step
):
    group = _cfg_group(tmp_path, (_ci("alpha-x", "alpha"),))
    _fake_snapshot_ls(mocker)
    child = mocker.patch.object(dashboard.subprocess, "run")
    render = mocker.patch.object(dashboard, "render", wraps=dashboard.render)

    keys = [*_container_menu_keys(group, "snapshots"), *_TO_SNAPSHOT_ROW]
    if step == "confirm":
        keys += [_ENTER, b"j"]  # Restore, then onto "Yes"
    assert _drive_with_vanish(mocker, keys, [group], group.containers.clear, when=when) == 0

    purposes = {p.purpose for p in _rendered(render, dashboard.Picker)}
    assert "container-snapshot-action" in purposes
    assert ("container-snapshot-confirm" in purposes) == (step == "confirm")
    child.assert_not_called()
    assert "'alpha-x' is gone" in " ".join(str(n) for n in _notices(render))


_THREE_SNAPS = json.dumps([{"name": n, "created": "2026-09-29T10:00:00Z"} for n in ("a", "b", "c")])


@pytest.mark.parametrize("verb", ["restore", "delete"])
@pytest.mark.parametrize(("row", "tag"), [(0, "a"), (1, "b"), (2, "c")], ids=["a", "b", "c"])
def test_snapshot_change_acts_on_the_chosen_snapshot_not_the_first(
    mocker, tmp_path, verb, row, tag
):
    group = _cfg_group(tmp_path, (_ci("alpha-x", "alpha"),))
    _fake_snapshot_ls(mocker, dashboard.da.CliResult(True, "done", _THREE_SNAPS))
    child = mocker.patch.object(dashboard.subprocess, "run")
    child.return_value.returncode = 0
    mocker.patch.object(dashboard, "_wait_for_return")

    keys = [
        *_container_menu_keys(group, "snapshots"),
        *[b"j"] * (2 + row),  # past the two create entries
        _ENTER,
        *([b"j"] if verb == "delete" else []),
        _ENTER,
        b"j",
        _ENTER,
    ]
    assert _drive_run(mocker, keys, [group]) == 0

    child.assert_called_once_with(
        ["jailbee", "snapshot", verb, "--config", str(group.config_path), "--", "alpha-x", tag],
        check=False,
        cwd=tmp_path,
    )


@pytest.mark.parametrize("verb", ["restore", "delete"])
def test_snapshot_named_config_stays_positional_over_ssh(mocker, tmp_path, verb):
    from jailbee.config.models_remote import RemoteSSHConfig

    group = _cfg_group(tmp_path, (_ci("alpha-x", "alpha"),))
    listing = json.dumps([{"name": "--config", "created": "2026-09-29T10:00:00Z"}])
    _fake_snapshot_ls(mocker, dashboard.da.CliResult(True, "done", listing))
    child = mocker.patch.object(dashboard.subprocess, "run")
    child.return_value.returncode = 0
    mocker.patch.object(dashboard, "_wait_for_return")
    kwargs = {"remote": True, "over_ssh": True, "ssh_policy": RemoteSSHConfig()}

    keys = [
        *_container_menu_keys(group, "snapshots", **kwargs),
        *_TO_SNAPSHOT_ROW,
        *([b"j"] if verb == "delete" else []),
        _ENTER,
        b"j",
        _ENTER,
    ]
    assert _drive_run(mocker, keys, [group], **kwargs) == 0

    child.assert_called_once_with(
        ["jailbee", "snapshot", verb, "--", "alpha-x", "--config"], check=False, cwd=tmp_path
    )


def test_an_unknown_snapshot_action_spawns_nothing(mocker, tmp_path):
    group = _cfg_group(tmp_path, (_ci("alpha-x", "alpha"),))
    _fake_snapshot_ls(mocker)
    child = mocker.patch.object(dashboard.subprocess, "run")
    real = dashboard.dact.snapshot_confirm_picker
    mocker.patch.object(
        dashboard.dact,
        "snapshot_confirm_picker",
        side_effect=lambda container, _action, tag: real(container, "bogus", tag),
    )

    keys = [*_container_menu_keys(group, "snapshots"), *_TO_SNAPSHOT_ROW, _ENTER, b"j", _ENTER]
    assert _drive_run(mocker, keys, [group]) == 0

    child.assert_not_called()


# --- Outbox: the same picker panels as Snapshots… ------------------------------

_OUTBOX_JSON = json.dumps(
    {
        "schema": 1,
        "containers": [
            {
                "name": "alpha-x",
                "available": True,
                "error": None,
                "stores": [],
                "proposals": [
                    {
                        "id": "pr/a.json",
                        "state": "pending",
                        "revision": "r1",
                        "actions": [{"index": 0}],
                        "error": None,
                        "edit_block": None,
                    }
                ],
            }
        ],
    }
)
_OUTBOX_LS = ["outbox", "ls", "alpha-x", "-o", "json"]
_TO_PROPOSAL = [_ENTER]  # the first entry is the only proposal


def _fake_outbox_ls(mocker, result=None):
    """Patch the quiet runner the outbox listing (and a delete) go through."""
    return mocker.patch.object(
        dashboard.da,
        "run_cli_quiet",
        return_value=result or dashboard.da.CliResult(True, "done", _OUTBOX_JSON),
    )


@pytest.mark.parametrize("over_ssh", [False, True], ids=["local", "ssh-default"])
def test_outbox_lists_quietly_in_a_picker_instead_of_the_browser(mocker, tmp_path, over_ssh):
    from jailbee.config.models_remote import RemoteSSHConfig

    group = _cfg_group(tmp_path, (_ci("alpha-x", "alpha"),))
    policy = RemoteSSHConfig() if over_ssh else None
    listing = _fake_outbox_ls(mocker)
    child = mocker.patch.object(dashboard.subprocess, "run")
    render = mocker.patch.object(dashboard, "render", wraps=dashboard.render)
    kwargs = {"remote": over_ssh, "over_ssh": over_ssh, "ssh_policy": policy}

    keys = _container_menu_keys(group, "outbox browse", **kwargs)
    assert _drive_run(mocker, keys, [group], **kwargs) == 0

    flags = [] if over_ssh else ["--config", str(group.config_path)]
    listing.assert_called_once_with([*_OUTBOX_LS, *flags], cwd=tmp_path)
    picker = _rendered(render, dashboard.Picker)[0]
    assert picker.title == "Outbox — alpha-x"
    assert [e.value for e in picker.entries] == ["proposal:pr/a.json", "browse"]
    child.assert_not_called()  # listing is quiet: the screen never blanked


def test_outbox_proposal_show_is_paged_in_the_terminal(mocker, tmp_path):
    group = _cfg_group(tmp_path, (_ci("alpha-x", "alpha"),))
    _fake_outbox_ls(mocker)
    run_cli = mocker.patch.object(dashboard, "_run_cli_foreground", return_value=0)
    render = mocker.patch.object(dashboard, "render", wraps=dashboard.render)

    keys = [*_container_menu_keys(group, "outbox browse"), *_TO_PROPOSAL, _ENTER]
    assert _drive_run(mocker, keys, [group]) == 0

    actions = [
        p for p in _rendered(render, dashboard.Picker) if p.purpose == "container-outbox-proposal"
    ]
    assert [e.value for e in actions[0].entries] == ["show", "publish", "delete"]
    run_cli.assert_called_once()
    assert run_cli.call_args.args[1] == ["outbox", "show", "alpha-x", "pr/a.json", "--color"]
    assert run_cli.call_args.kwargs["style"] == "paged"


def test_outbox_publish_leaves_plan_confirmation_to_the_terminal(mocker, tmp_path):
    group = _cfg_group(tmp_path, (_ci("alpha-x", "alpha"),))
    _fake_outbox_ls(mocker)
    child = mocker.patch.object(dashboard.subprocess, "run")
    child.return_value.returncode = 0
    wait = mocker.patch.object(dashboard, "_wait_for_return")

    keys = [
        *_container_menu_keys(group, "outbox browse"),
        *_TO_PROPOSAL,
        b"j",  # Publish…
        _ENTER,
        b"j",  # "Yes, publish"
        _ENTER,
    ]
    assert _drive_run(mocker, keys, [group]) == 0

    child.assert_called_once_with(
        [
            "jailbee",
            "outbox",
            "apply",
            "alpha-x",
            "pr/a.json",
            "--revision",
            "r1",
            "--config",
            str(group.config_path),
        ],
        check=False,
        cwd=tmp_path,
    )
    wait.assert_called_once()


def test_outbox_delete_runs_quietly_after_a_yes(mocker, tmp_path):
    group = _cfg_group(tmp_path, (_ci("alpha-x", "alpha"),))
    quiet = _fake_outbox_ls(mocker)
    child = mocker.patch.object(dashboard.subprocess, "run")

    keys = [
        *_container_menu_keys(group, "outbox browse"),
        *_TO_PROPOSAL,
        b"j",
        b"j",  # Delete…
        _ENTER,
        b"j",  # "Yes, delete"
        _ENTER,
    ]
    assert _drive_run(mocker, keys, [group]) == 0

    flags = ["--config", str(group.config_path)]
    assert quiet.call_args_list[-1] == mocker.call(
        ["outbox", "drop", "alpha-x", "pr/a.json", "--yes", "--revision", "r1", *flags],
        cwd=tmp_path,
    )
    child.assert_not_called()


@pytest.mark.parametrize("downs", [1, 2], ids=["publish", "delete"])
def test_outbox_confirm_no_runs_nothing(mocker, tmp_path, downs):
    group = _cfg_group(tmp_path, (_ci("alpha-x", "alpha"),))
    quiet = _fake_outbox_ls(mocker)
    child = mocker.patch.object(dashboard.subprocess, "run")
    render = mocker.patch.object(dashboard, "render", wraps=dashboard.render)

    keys = [
        *_container_menu_keys(group, "outbox browse"),
        *_TO_PROPOSAL,
        *[b"j"] * downs,
        _ENTER,
        _ENTER,  # a stray Enter lands on "No"
    ]
    assert _drive_run(mocker, keys, [group]) == 0

    child.assert_not_called()
    assert quiet.call_count == 1  # only the listing
    assert "Cancelled" in _notices(render)


def test_outbox_browse_entry_hands_the_terminal_to_the_full_browser(mocker, tmp_path):
    group = _cfg_group(tmp_path, (_ci("alpha-x", "alpha"),))
    _fake_outbox_ls(mocker)
    child = mocker.patch.object(dashboard.subprocess, "run")
    child.return_value.returncode = 0
    wait = mocker.patch.object(dashboard, "_wait_for_return")

    keys = [*_container_menu_keys(group, "outbox browse"), b"j", _ENTER]
    assert _drive_run(mocker, keys, [group]) == 0

    child.assert_called_once_with(
        ["jailbee", "outbox", "browse", "alpha-x", "--config", str(group.config_path)],
        check=False,
        cwd=tmp_path,
    )
    wait.assert_not_called()  # interactive: nothing left on screen to read


@pytest.mark.parametrize(
    ("result", "notice"),
    [
        (dashboard.da.CliResult(False, "error: boom"), "could not list the outbox: error: boom"),
        (
            dashboard.da.CliResult(True, "done", "Container  Proposal"),
            "could not list the outbox: unexpected output from 'jailbee outbox ls'",
        ),
        (
            dashboard.da.CliResult(
                False,
                "exit 2",
                json.dumps(
                    {"containers": [{"name": "alpha-x", "available": False, "error": "stopped"}]}
                ),
            ),
            "could not read the outbox: stopped",
        ),
        (
            dashboard.da.CliResult(True, "done", json.dumps({"containers": []})),
            "Outbox of 'alpha-x' is empty",
        ),
    ],
    ids=["cli-failed", "not-json", "unavailable", "empty"],
)
def test_an_outbox_with_nothing_to_offer_is_a_notice(mocker, tmp_path, result, notice):
    group = _cfg_group(tmp_path, (_ci("alpha-x", "alpha"),))
    _fake_outbox_ls(mocker, result)
    render = mocker.patch.object(dashboard, "render", wraps=dashboard.render)

    assert _drive_run(mocker, _container_menu_keys(group, "outbox browse"), [group]) == 0

    assert not _rendered(render, dashboard.Picker)
    assert notice in _notices(render)


def test_outbox_hides_what_the_ssh_allowlist_does_not_permit(mocker, tmp_path):
    from jailbee.config.models_remote import RemoteSSHConfig

    group = _cfg_group(tmp_path, (_ci("alpha-x", "alpha"),))
    policy = RemoteSSHConfig.model_validate(
        {"commands": {"mode": "allowlist", "allow": ["shell", "outbox browse", "outbox ls"]}}
    )
    _fake_outbox_ls(mocker)
    render = mocker.patch.object(dashboard, "render", wraps=dashboard.render)
    kwargs = {"remote": True, "over_ssh": True, "ssh_policy": policy}

    keys = [*_container_menu_keys(group, "outbox browse", **kwargs), *_TO_PROPOSAL]
    assert _drive_run(mocker, keys, [group], **kwargs) == 0

    picker = _rendered(render, dashboard.Picker)[0]
    assert [e.value for e in picker.entries] == ["proposal:pr/a.json", "browse"]
    assert "Nothing can be done to pr/a.json here" in _notices(render)


# --- Mount… / Unmount… -------------------------------------------------------


def _mount_group(tmp_path: Path) -> dashboard.RepoGroup:
    """Kinds aws + gcloud configured; gcloud attached to alpha-x."""
    group = _cfg_group(
        tmp_path, (dataclasses.replace(_ci("alpha-x", "alpha"), optional_mounts=("gcloud",)),)
    )
    group.optional_mounts = ("aws", "gcloud")
    return group


@pytest.mark.parametrize(
    ("over_ssh", "policy_kwargs", "expected"),
    [
        (False, None, {"mount-add", "mount-remove"}),
        (False, {"commands": {"mode": "disabled"}}, {"mount-add", "mount-remove"}),
        (True, {}, {"mount-remove"}),
        (True, {"commands": {"mode": "allowlist", "allow": ["shell", "mount"]}}, set()),
        (
            True,
            {
                "commands": {"mode": "allowlist", "allow": ["shell", "mount"]},
                "restrict_host": False,
            },
            {"mount-add"},
        ),
        (
            True,
            {"commands": {"mode": "allowlist", "allow": ["shell", "unmount"]}},
            {"mount-remove"},
        ),
        (True, {"commands": {"mode": "allowlist", "allow": ["shell"]}}, set()),
        (True, {"excluded_repos": ["other"]}, set()),
    ],
    ids=[
        "local",
        "local-ignores-policy",
        "ssh-default",
        "ssh-allowlist-mount-restricted",
        "ssh-allowlist-mount-unrestricted",
        "ssh-allowlist-unmount",
        "ssh-allowlist-without",
        "ssh-excluded-repos",
    ],
)
def test_container_menu_mount_entries_follow_the_ssh_policy(
    tmp_path, over_ssh, policy_kwargs, expected
):
    menu = dashboard.open_menu(
        [_mount_group(tmp_path)],
        "alpha-x",
        remote=over_ssh,
        over_ssh=over_ssh,
        ssh_policy=_ssh_policy(policy_kwargs),
    )
    assert menu is not None
    assert {verb for _label, verb in menu.actions} & {"mount-add", "mount-remove"} == expected


@pytest.mark.parametrize(
    ("group_factory", "allow", "expected"),
    [
        (lambda tp: _cfg_group(tp, (_ci("alpha-x", "alpha"),)), ["snapshot ls"], ["Snapshots…"]),
        (_mount_group, ["unmount"], ["Unmount…"]),
        (lambda tp: _cfg_group(tp, (_ci("alpha-x", "alpha"),)), ["snapshot create"], None),
        (_mount_group, ["stats"], None),
    ],
    ids=["snapshot-ls-only", "unmount-only", "nothing-relevant-snapshot", "nothing-relevant"],
)
def test_container_menu_survives_an_empty_shared_action_list(
    tmp_path, group_factory, allow, expected
):
    """No lifecycle or shell verb is permitted, yet a terminal-only entry may be."""
    policy = _ssh_policy({"commands": {"mode": "allowlist", "allow": allow}})
    group = group_factory(tmp_path)
    kwargs = {"remote": True, "over_ssh": True, "ssh_policy": policy}
    assert dashboard.actions_for_container([group], "alpha-x", **kwargs) == []

    menu = dashboard.open_menu([group], "alpha-x", **kwargs)

    if expected is None:
        assert menu is None
    else:
        assert menu is not None
        assert [label for label, _verb in menu.actions] == expected


def test_mount_offers_the_unattached_kinds_and_runs_quietly(mocker, tmp_path):
    group = _mount_group(tmp_path)
    quiet = mocker.patch.object(
        dashboard.da,
        "run_cli_quiet",
        return_value=dashboard.da.CliResult(True, "✓ Mounted 'aws' in container 'x'"),
    )
    child = mocker.patch.object(dashboard.subprocess, "run")
    render = mocker.patch.object(dashboard, "render", wraps=dashboard.render)

    keys = [*_container_menu_keys(group, "mount-add"), _ENTER]
    assert _drive_run(mocker, keys, [group]) == 0

    assert [e.value for e in _rendered(render, dashboard.Picker)[0].entries] == ["aws"]
    quiet.assert_called_once_with(
        ["mount", "--config", str(group.config_path), "--", "aws", "alpha-x"], cwd=tmp_path
    )
    child.assert_not_called()  # quiet: the screen never blanked
    assert "✓ Mounted 'aws' in container 'x'" in _notices(render)


def test_unmount_offers_the_attached_kinds(mocker, tmp_path):
    group = _mount_group(tmp_path)
    quiet = mocker.patch.object(
        dashboard.da, "run_cli_quiet", return_value=dashboard.da.CliResult(True, "done")
    )
    render = mocker.patch.object(dashboard, "render", wraps=dashboard.render)

    keys = [*_container_menu_keys(group, "mount-remove"), _ENTER]
    assert _drive_run(mocker, keys, [group]) == 0

    assert [e.value for e in _rendered(render, dashboard.Picker)[0].entries] == ["gcloud"]
    quiet.assert_called_once_with(
        ["unmount", "--config", str(group.config_path), "--", "gcloud", "alpha-x"], cwd=tmp_path
    )


def test_unmount_over_ssh_offers_the_attached_kinds_without_config(mocker, tmp_path):
    from jailbee.config.models_remote import RemoteSSHConfig

    group = _mount_group(tmp_path)
    policy = RemoteSSHConfig()
    quiet = mocker.patch.object(
        dashboard.da, "run_cli_quiet", return_value=dashboard.da.CliResult(True, "done")
    )
    render = mocker.patch.object(dashboard, "render", wraps=dashboard.render)
    kwargs = {"remote": True, "over_ssh": True, "ssh_policy": policy}

    keys = [*_container_menu_keys(group, "mount-remove", **kwargs), _ENTER]
    assert _drive_run(mocker, keys, [group], **kwargs) == 0

    assert [e.value for e in _rendered(render, dashboard.Picker)[0].entries] == ["gcloud"]
    quiet.assert_called_once_with(["unmount", "--", "gcloud", "alpha-x"], cwd=tmp_path)


def test_a_refused_mount_is_a_long_notice(mocker, tmp_path):
    group = _mount_group(tmp_path)
    mocker.patch.object(
        dashboard.da,
        "run_cli_quiet",
        return_value=dashboard.da.CliResult(False, "error: Unknown optional mount 'aws'"),
    )
    child = mocker.patch.object(dashboard.subprocess, "run")
    render = mocker.patch.object(dashboard, "render", wraps=dashboard.render)

    keys = [*_container_menu_keys(group, "mount-add"), _ENTER]
    assert _drive_run(mocker, keys, [group]) == 0

    # the CLI's own verdict, shown whole; the dashboard is still running (rc 0)
    assert "error: Unknown optional mount 'aws'" in _notices(render)
    child.assert_not_called()


def test_mount_picker_escape_runs_nothing(mocker, tmp_path):
    group = _mount_group(tmp_path)
    quiet = mocker.patch.object(dashboard.da, "run_cli_quiet")
    render = mocker.patch.object(dashboard, "render", wraps=dashboard.render)

    keys = [*_container_menu_keys(group, "mount-add"), _ESC]
    assert _drive_run(mocker, keys, [group]) == 0

    assert _rendered(render, dashboard.Picker)  # it did open
    quiet.assert_not_called()


@pytest.mark.parametrize("verb", ["mount-add", "mount-remove"])
def test_a_kind_spelled_like_an_option_stays_positional(mocker, tmp_path, verb):
    group = _mount_group(tmp_path)
    group.optional_mounts = ("--yes", "gcloud")
    quiet = mocker.patch.object(
        dashboard.da, "run_cli_quiet", return_value=dashboard.da.CliResult(True, "done")
    )
    if verb == "mount-remove":
        group.containers[0] = dataclasses.replace(
            group.containers[0], optional_mounts=("--yes", "gcloud")
        )

    keys = [*_container_menu_keys(group, verb), _ENTER]  # the first entry: "--yes"
    assert _drive_run(mocker, keys, [group]) == 0

    verb_word = "unmount" if verb == "mount-remove" else "mount"
    quiet.assert_called_once_with(
        [verb_word, "--config", str(group.config_path), "--", "--yes", "alpha-x"], cwd=tmp_path
    )


def test_kinds_missing_from_the_config_are_not_offered_to_unmount(mocker, tmp_path):
    group = _mount_group(tmp_path)
    group.containers[0] = dataclasses.replace(
        group.containers[0], optional_mounts=("gcloud", "retired")
    )
    mocker.patch.object(
        dashboard.da, "run_cli_quiet", return_value=dashboard.da.CliResult(True, "done")
    )
    render = mocker.patch.object(dashboard, "render", wraps=dashboard.render)

    keys = [*_container_menu_keys(group, "mount-remove"), _ESC]
    assert _drive_run(mocker, keys, [group]) == 0

    assert [e.value for e in _rendered(render, dashboard.Picker)[0].entries] == ["gcloud"]


def _drive_mount_menu_then(mocker, group, verb, change, *, same_read: bool = False) -> None:
    """Open the container menu on ``verb``, run ``change()``, then press Enter on it.

    The menu overlay keeps the entries it opened with, so this is a stale menu.
    With ``same_read`` the change lands on the very read that delivers Enter, so
    no frame sees it first.
    """
    keys = _container_menu_keys(group, verb)
    script = iter([*keys[:-1], "change", *([] if same_read else [keys[-1]])])

    def read(_fd, _n):
        item = next(script, b"\x03")
        if item == "change":
            change()
            return keys[-1] if same_read else b"z"
        return item

    assert _drive_run_with_reader(mocker, read, [group]) == 0


def test_mount_with_nothing_left_to_add_notices_instead_of_opening(mocker, tmp_path):
    group = _mount_group(tmp_path)
    quiet = mocker.patch.object(dashboard.da, "run_cli_quiet")
    render = mocker.patch.object(dashboard, "render", wraps=dashboard.render)

    def attach_everything():
        group.containers[0] = dataclasses.replace(
            group.containers[0], optional_mounts=("aws", "gcloud")
        )

    _drive_mount_menu_then(mocker, group, "mount-add", attach_everything)

    assert not _rendered(render, dashboard.Picker)
    assert "No optional mount to add" in _notices(render)
    quiet.assert_not_called()


def test_unmount_with_nothing_attached_notices_instead_of_opening(mocker, tmp_path):
    group = _mount_group(tmp_path)
    quiet = mocker.patch.object(dashboard.da, "run_cli_quiet")
    render = mocker.patch.object(dashboard, "render", wraps=dashboard.render)

    def detach_everything():
        group.containers[0] = dataclasses.replace(group.containers[0], optional_mounts=())

    _drive_mount_menu_then(mocker, group, "mount-remove", detach_everything)

    assert not _rendered(render, dashboard.Picker)
    assert "No optional mount to remove" in _notices(render)
    quiet.assert_not_called()


@pytest.mark.parametrize("verb", ["mount-add", "mount-remove"])
def test_container_vanishing_on_the_read_that_opens_the_mount_picker_runs_nothing(
    mocker, tmp_path, verb
):
    group = _mount_group(tmp_path)
    quiet = mocker.patch.object(dashboard.da, "run_cli_quiet")
    render = mocker.patch.object(dashboard, "render", wraps=dashboard.render)

    _drive_mount_menu_then(mocker, group, verb, group.containers.clear, same_read=True)

    assert not _rendered(render, dashboard.Picker)
    quiet.assert_not_called()
    assert "'alpha-x' is gone" in " ".join(str(n) for n in _notices(render))


def test_stale_mount_menu_refused_by_the_policy_at_submit_runs_nothing(mocker, tmp_path):
    group = _mount_group(tmp_path)
    quiet = mocker.patch.object(dashboard.da, "run_cli_quiet")
    render = mocker.patch.object(dashboard, "render", wraps=dashboard.render)
    real_check = dashboard.check_dashboard_command

    def refuse(argv, policy, *, over_ssh):
        if argv[:1] == ["mount"]:
            raise dashboard.RouteError("mount is not permitted")
        return real_check(argv, policy, over_ssh=over_ssh)

    mocker.patch.object(dashboard, "check_dashboard_command", side_effect=refuse)

    keys = [*_container_menu_keys(group, "mount-add"), _ENTER]
    assert _drive_run(mocker, keys, [group]) == 0

    assert _rendered(render, dashboard.Picker)
    quiet.assert_not_called()
    assert "mount is not permitted" in _notices(render)


@_VANISH_WHEN
@pytest.mark.parametrize("verb", ["mount-add", "mount-remove"])
def test_container_vanishing_while_the_mount_picker_is_open_runs_nothing(
    mocker, tmp_path, when, verb
):
    group = _mount_group(tmp_path)
    quiet = mocker.patch.object(dashboard.da, "run_cli_quiet")
    child = mocker.patch.object(dashboard.subprocess, "run")
    render = mocker.patch.object(dashboard, "render", wraps=dashboard.render)

    keys = _container_menu_keys(group, verb)
    assert _drive_with_vanish(mocker, keys, [group], group.containers.clear, when=when) == 0

    purpose = "container-mount-remove" if verb == "mount-remove" else "container-mount-add"
    assert {p.purpose for p in _rendered(render, dashboard.Picker)} == {purpose}
    quiet.assert_not_called()
    child.assert_not_called()
    assert "'alpha-x' is gone" in " ".join(str(n) for n in _notices(render))


def test_help_panel_points_at_the_repo_and_container_menu_entries():
    text = _render_text(dashboard._render_help())
    assert "Apply config…" in text
    assert "Snapshots…" in text


def _every_verb_group(tmp_path: Path) -> dashboard.RepoGroup:
    """A container that is offered every terminal-only entry at once."""
    group = _mount_group(tmp_path)
    group.containers[0] = dataclasses.replace(
        group.containers[0], job_phase="autostart", job_pid=os.getpid(), job_kind="autostart"
    )
    return group


_CONTAINER_VERB_CASES = sorted(dashboard.dact.CONTAINER_VERBS)


def test_every_container_verb_has_a_guard_case(tmp_path):
    group = _every_verb_group(tmp_path)
    menu = dashboard.open_menu([group], "alpha-x")
    assert menu is not None
    offered = {verb for _label, verb in menu.actions}
    # a new verb must be offered by this fixture (and so parametrized below)
    assert offered & dashboard.dact.CONTAINER_VERBS == dashboard.dact.CONTAINER_VERBS
    assert set(_CONTAINER_VERB_CASES) == dashboard.dact.CONTAINER_VERBS


@pytest.mark.parametrize("verb", _CONTAINER_VERB_CASES)
def test_a_terminal_only_container_entry_never_reaches_the_shared_dispatcher(
    mocker, tmp_path, verb
):
    """Each entry opens an overlay or spawns through the dashboard's own runners.

    It must never fall through to ``_dispatch_action``, which would build the
    invalid ``jailbee <verb> <container>`` command.
    """
    group = _every_verb_group(tmp_path)
    dispatch = mocker.patch.object(dashboard, "_dispatch_action")
    child = mocker.patch.object(dashboard.subprocess, "run")
    child.return_value.returncode = 0
    mocker.patch.object(dashboard, "_wait_for_return")
    # one quiet runner: the snapshot listing needs JSON, the mount a plain success
    mocker.patch.object(
        dashboard.da,
        "run_cli_quiet",
        return_value=dashboard.da.CliResult(True, "done", _SNAPS_JSON),
    )
    render = mocker.patch.object(dashboard, "render", wraps=dashboard.render)

    assert _drive_run(mocker, [*_container_menu_keys(group, verb), _ENTER], [group]) == 0

    dispatch.assert_not_called()
    opened = _rendered(render, dashboard.Picker) or _rendered(render, dashboard.TextPrompt)
    spawned = [call.args[0] for call in child.call_args_list]
    assert opened or spawned
    assert ["jailbee", verb, "alpha-x"] not in spawned
    assert all(argv[:2] != ["jailbee", verb] for argv in spawned)


def test_outbox_dispatch_rechecks_ssh_policy(mocker, tmp_path):
    from jailbee.config.models_remote import RemoteCommandPolicy, RemoteSSHConfig
    from jailbee.remote_ssh.router import RouteError

    child = mocker.patch.object(dashboard.subprocess, "run")
    policy = RemoteSSHConfig(commands=RemoteCommandPolicy(mode="allowlist", allow=["git merge"]))
    with pytest.raises(RouteError):
        dashboard._dispatch_action(
            _dispatch_target(tmp_path), "outbox browse", "alpha-x", over_ssh=True, ssh_policy=policy
        )
    child.assert_not_called()


def test_gui_remote_action_menu_offers_app_launches():
    """`remote.ssh.gui` lets a remote session launch apps onto the shared display."""
    apps = [dashboard.AppMenuEntry("chrome", "Chrome")]
    ctx = _ctx(apps=apps, remote=True)
    assert not [a for a in dashboard.menu_actions(ctx) if a[1] == "chrome"]

    enabled = dashboard.menu_actions(dataclasses.replace(ctx, gui_remote=True))

    assert ("Launch Chrome", "chrome") in enabled


def test_gui_remote_quick_key_reason_follows_the_gui_switch():
    from jailbee.config.models_remote import RemoteCommandPolicy, RemoteSSHConfig

    group = dashboard.RepoGroup("alpha", "/repos/alpha", None, [_ci("alpha-x", "alpha")])
    group.apps = _apps("chrome")
    off = RemoteSSHConfig(commands=RemoteCommandPolicy(mode="full"))
    on = RemoteSSHConfig(commands=RemoteCommandPolicy(mode="full"), gui=True)
    kwargs = {"remote": True, "over_ssh": True}

    assert (
        dashboard.quick_verb([group], "alpha-x", "action:chrome", ssh_policy=off, **kwargs) is None
    )
    note = dashboard.quick_reject_note(
        [group], "alpha-x", "action:chrome", ssh_policy=off, **kwargs
    )
    assert note == "GUI apps are not available over remote SSH"
    assert (
        dashboard.quick_verb([group], "alpha-x", "action:chrome", ssh_policy=on, **kwargs)
        == "chrome"
    )
    note = dashboard.quick_reject_note([group], "alpha-x", "action:chrome", ssh_policy=on, **kwargs)
    assert "GUI apps are not available" not in note


def test_gui_remote_allowlist_without_chrome_does_not_offer_chrome():
    """The feature switch is not enough: the command policy still decides."""
    from jailbee.config.models_remote import RemoteCommandPolicy, RemoteSSHConfig

    group = dashboard.RepoGroup("alpha", "/repos/alpha", None, [_ci("alpha-x", "alpha")])
    group.apps = _apps("chrome")
    policy = RemoteSSHConfig(gui=True, commands=RemoteCommandPolicy(mode="allowlist", allow=["ls"]))

    actions = dashboard.actions_for_container(
        [group], "alpha-x", remote=True, ssh_policy=policy, over_ssh=True
    )

    assert "chrome" not in {verb for _label, verb in actions}


def test_gui_remote_allowlist_naming_chrome_offers_chrome():
    from jailbee.config.models_remote import RemoteCommandPolicy, RemoteSSHConfig

    group = dashboard.RepoGroup("alpha", "/repos/alpha", None, [_ci("alpha-x", "alpha")])
    group.apps = _apps("chrome")
    policy = RemoteSSHConfig(
        gui=True, commands=RemoteCommandPolicy(mode="allowlist", allow=["chrome"])
    )

    actions = dashboard.actions_for_container(
        [group], "alpha-x", remote=True, ssh_policy=policy, over_ssh=True
    )

    assert "chrome" in {verb for _label, verb in actions}


@pytest.mark.parametrize(
    ("verb", "pauses"),
    [("chrome", True), ("apps run figma", True), ("shell", False)],
)
def test_remote_gui_dispatch_keeps_the_recipe_on_screen(mocker, tmp_path, verb, pauses):
    from jailbee.config.models_remote import RemoteCommandPolicy, RemoteSSHConfig

    run = mocker.patch.object(dashboard.subprocess, "run")
    run.return_value.returncode = 0
    wait = mocker.patch.object(dashboard, "_wait_for_return")
    policy = RemoteSSHConfig(commands=RemoteCommandPolicy(mode="full"), gui=True)

    dashboard._dispatch_action(
        _dispatch_target(tmp_path),
        verb,
        "alpha-x",
        remote=True,
        over_ssh=True,
        ssh_policy=policy,
    )

    run.assert_called_once()
    assert wait.called is pauses


def test_is_gui_verb():
    assert dashboard._is_gui_verb("chrome")
    assert dashboard._is_gui_verb("apps run figma")
    assert not dashboard._is_gui_verb("shell")


def _gui_dispatch_policy(*, gui: bool):
    from jailbee.config.models_remote import RemoteCommandPolicy, RemoteSSHConfig

    return RemoteSSHConfig(commands=RemoteCommandPolicy(mode="full"), gui=gui, restrict_host=False)


def test_dispatch_action_pauses_after_a_gui_launch_over_ssh_when_gui_is_on(mocker, tmp_path):
    """The launch prints how to reach the shared display; the pause keeps it readable."""
    run = mocker.patch.object(dashboard.subprocess, "run")
    run.return_value.returncode = 0
    wait = mocker.patch.object(dashboard, "_wait_for_return")

    dashboard._dispatch_action(
        _dispatch_target(tmp_path),
        "chrome",
        "alpha-x",
        over_ssh=True,
        ssh_policy=_gui_dispatch_policy(gui=True),
    )

    wait.assert_called_once_with()


def test_dispatch_action_does_not_pause_after_a_gui_launch_in_a_waypipe_session(
    mocker, monkeypatch, tmp_path
):
    """A waypipe session has no RDP recipe to read: the window opens on the laptop."""
    from jailbee.remote_ssh.session import WaypipeSession, child_environment

    env = child_environment({}, gui_port=2222, waypipe=WaypipeSession("0a1b2c3d", "lz4"))
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    run = mocker.patch.object(dashboard.subprocess, "run")
    run.return_value.returncode = 0
    wait = mocker.patch.object(dashboard, "_wait_for_return")

    dashboard._dispatch_action(
        _dispatch_target(tmp_path),
        "chrome",
        "alpha-x",
        over_ssh=True,
        ssh_policy=_gui_dispatch_policy(gui=True),
    )

    wait.assert_not_called()


def test_dispatch_action_does_not_pause_after_a_gui_verb_when_gui_is_off(mocker, tmp_path):
    run = mocker.patch.object(dashboard.subprocess, "run")
    run.return_value.returncode = 0
    wait = mocker.patch.object(dashboard, "_wait_for_return")

    dashboard._dispatch_action(
        _dispatch_target(tmp_path),
        "chrome",
        "alpha-x",
        over_ssh=True,
        ssh_policy=_gui_dispatch_policy(gui=False),
    )

    wait.assert_not_called()


def test_window_rows_keeps_everything_that_fits():
    assert dashboard.window_rows([1, 1, 1], 2, 3) == dashboard.TableWindow(0, 3, 0, 0)


def test_window_rows_is_top_anchored_while_the_cursor_fits_there():
    # Four rows plus a "↓ 6 more" marker fill the five-line budget.
    assert dashboard.window_rows([1] * 10, 1, 5) == dashboard.TableWindow(0, 4, 0, 6)


def test_window_rows_is_bottom_anchored_near_the_end():
    assert dashboard.window_rows([1] * 10, 9, 5) == dashboard.TableWindow(6, 10, 6, 0)


def test_window_rows_centres_the_cursor_between_two_markers():
    w = dashboard.window_rows([1] * 20, 10, 7)
    assert w.start <= 10 < w.stop
    assert w.stop - w.start == 5  # seven lines minus two markers
    assert (w.hidden_above, w.hidden_below) == (w.start, 20 - w.stop)


def test_window_rows_counts_wrapped_rows_by_their_height():
    heights = [1, 2, 2, 2, 2, 2, 2]
    w = dashboard.window_rows(heights, 6, 6)
    assert w.start <= 6 < w.stop
    assert sum(heights[w.start : w.stop]) + (w.hidden_above > 0) + (w.hidden_below > 0) <= 6


def test_window_rows_without_a_cursor_starts_at_the_top():
    assert dashboard.window_rows([1] * 10, None, 5).start == 0


def _named_rows_group(tmp_path, n: int) -> dashboard.RepoGroup:
    """Containers whose short names ("row07") cannot collide with anything else."""
    return dashboard.RepoGroup(
        "alpha",
        "/repos/alpha",
        tmp_path / "a.yaml",
        [_ci(f"alpha-row{i:02d}", "alpha") for i in range(n)],
    )


def test_render_scrolls_a_long_table_to_the_cursor(tmp_path):
    frame = dashboard.render(
        [_named_rows_group(tmp_path, 40)],
        dashboard.Row("container", "alpha-row35"),
        now=datetime(2026, 6, 8, 12, 0, tzinfo=UTC),
        git_enabled=True,
        height=20,
    )
    lines = _render_text(frame, width=100).splitlines()
    text = "\n".join(lines)
    assert len(lines) <= 20
    assert "NAME" in text  # the column header stays pinned
    assert "row35" in text
    assert "row00" not in text
    assert "↑" in text and "more" in text
    assert lines[-1].startswith("╰")


def test_render_without_height_draws_a_long_table_whole(tmp_path):
    frame = dashboard.render(
        [_named_rows_group(tmp_path, 40)],
        dashboard.Row("container", "alpha-row35"),
        now=datetime(2026, 6, 8, 12, 0, tzinfo=UTC),
        git_enabled=True,
    )
    text = _render_text(frame, width=100)
    assert "row00" in text and "row39" in text and "more" not in text


_FRAME_NOW = datetime(2026, 6, 8, 12, 0, tzinfo=UTC)


def _frame(groups, selected, *, overlay=None, height=None, width=100, show_details=True):
    frame = dashboard.render(
        groups,
        selected,
        now=_FRAME_NOW,
        git_enabled=True,
        overlay=overlay,
        height=height,
        show_details=show_details,
    )
    return _render_text(frame, width=width).splitlines()


def test_details_panel_shows_under_the_table_for_the_highlighted_container(tmp_path):
    lines = _frame([_named_rows_group(tmp_path, 3)], dashboard.Row("container", "alpha-row01"))
    header = next(i for i, ln in enumerate(lines) if "NAME" in ln)
    title = next(i for i, ln in enumerate(lines) if "╭─ row01" in ln)
    assert title > header + 3  # under the heading and three container rows
    text = "\n".join(lines[title:])
    assert "network" in text and "git" in text and "state" in text


def test_details_toggled_off_draws_no_panel(tmp_path):
    lines = _frame(
        [_named_rows_group(tmp_path, 3)],
        dashboard.Row("container", "alpha-row01"),
        show_details=False,
    )
    assert "╭─ row01" not in "\n".join(lines)


def test_menu_sits_to_the_right_of_the_details(tmp_path):
    menu = dashboard.MenuState("alpha-row01", [("Shell", "shell"), ("Tmux", "tmux")])
    lines = _frame(
        [_named_rows_group(tmp_path, 3)],
        dashboard.Row("container", "alpha-row01"),
        overlay=menu,
        height=30,
    )
    shared = next(ln for ln in lines if "alpha-row01 →" in ln)
    details_at = shared.find("╭─ row01")  # the details title comes first
    menu_at = shared.find("alpha-row01 →")
    assert details_at < menu_at


def test_other_overlays_hide_the_details(tmp_path):
    picker = dashboard.Picker("x", "Pick one", (dashboard.PickerEntry("Entry 0", "0"),))
    lines = _frame(
        [_named_rows_group(tmp_path, 3)],
        dashboard.Row("container", "alpha-row01"),
        overlay=picker,
        height=30,
    )
    text = "\n".join(lines)
    assert "Pick one" in text
    assert "╭─ row01" not in text


def _with_activity(group: dashboard.RepoGroup, *, activity: bool) -> dashboard.RepoGroup:
    summary = AgentSummary(
        "claude",
        "busy",
        None,
        None,
        1,
        activity=AgentActivity("Bash  ls", "done") if activity else None,
    )
    return dataclasses.replace(
        group,
        containers=[dataclasses.replace(c, agent_status=(summary,)) for c in group.containers],
    )


def _details_content_rows(lines: list[str]) -> int:
    top = next(i for i, ln in enumerate(lines) if "╭─ row01" in ln)
    bottom = next(i for i in range(top + 1, len(lines)) if "╰" in lines[i])
    return bottom - top - 1


def test_the_details_panel_grows_by_the_activity_reservation(tmp_path):
    """A long table makes the panel fixed-height, so its rows are its cap."""
    selected = dashboard.Row("container", "alpha-row01")
    for activity, expected in (
        (True, dd.DETAILS_MAX_ROWS + dd.DETAILS_ACTIVITY_ROWS),
        (False, dd.DETAILS_MAX_ROWS),
    ):
        group = _with_activity(_named_rows_group(tmp_path, 40), activity=activity)
        lines = _frame([group], selected, height=40)
        assert _details_content_rows(lines) == expected, activity


def test_repo_heading_shows_the_repo_summary(tmp_path):
    text = "\n".join(_frame([_named_rows_group(tmp_path, 3)], dashboard.Row("repo", "alpha")))
    assert "/repos/alpha" in text and "3 running / 3" in text


def test_long_table_keeps_min_rows_and_the_cursor_with_details(tmp_path):
    lines = _frame(
        [_named_rows_group(tmp_path, 40)],
        dashboard.Row("container", "alpha-row39"),
        height=24,
    )
    assert len(lines) <= 24
    text = "\n".join(lines)
    assert "row39" in text and "↑" in text
    header = next(i for i, ln in enumerate(lines) if "NAME" in ln)
    panel_top = next(i for i, ln in enumerate(lines) if "╭─ row39" in ln)
    assert panel_top - header - 1 >= dashboard.MIN_TABLE_ROWS
    assert lines[-1].startswith("╰")


def test_short_terminals_never_overflow(tmp_path):
    menu = dashboard.MenuState(
        "alpha-row05", [(f"Action {i}", f"v{i}") for i in range(30)], index=20
    )
    for height in (8, 10, 12, 16):
        lines = _frame(
            [_named_rows_group(tmp_path, 40)],
            dashboard.Row("container", "alpha-row05"),
            overlay=menu,
            height=height,
        )
        assert len(lines) <= height, height
        assert lines[-1].startswith("╰"), height


def test_v_parses_to_the_details_toggle():
    assert dashboard.parse_key(b"v") == "details"


def test_v_toggles_the_details_panel_and_persists_it(mocker, tmp_path):
    saved = mocker.patch.object(dashboard, "save_view_state")
    render = mocker.spy(dashboard, "render")
    _drive_run(mocker, [b"v"], [_named_rows_group(tmp_path, 2)], view_state=dashboard.ViewState())
    assert render.call_args_list[0].kwargs["show_details"] is True
    assert render.call_args_list[-1].kwargs["show_details"] is False
    assert saved.call_args.args[2].show_details is False


def test_folding_keeps_a_stored_details_preference(mocker, tmp_path):
    """Every ViewState the loop saves must carry show_details, or a fold
    silently turns a hidden panel back on."""
    saved = mocker.patch.object(dashboard, "save_view_state")
    _drive_run(
        mocker,
        [b" "],  # the cursor starts on the repo heading: Space folds it
        [_named_rows_group(tmp_path, 2)],
        view_state=dashboard.ViewState(show_details=False),
    )
    state = saved.call_args.args[2]
    assert state.folded == frozenset({"alpha"})
    assert state.show_details is False


@pytest.mark.parametrize("path", ["fold", "settings", "repo-menu"])
def test_every_saved_view_state_keeps_a_stored_details_preference(mocker, tmp_path, path):
    """The Space fold, the settings overlay and the repo menu's Fold each save
    a whole ViewState; none may turn a stored-hidden panel back on."""
    group = _named_rows_group(tmp_path, 2)
    keys = {
        "fold": [b" "],
        "settings": [b"S", b" "],
        "repo-menu": _repo_menu_keys(group, "fold"),
    }[path]
    saved = mocker.patch.object(dashboard, "save_view_state")
    _drive_run(mocker, keys, [group], view_state=dashboard.ViewState(show_details=False))
    assert saved.call_count >= 1
    assert saved.call_args.args[2].show_details is False


def _mixed_group(tmp_path, n=40):
    """A repo whose containers differ in how much their panels have to say."""
    containers = [
        _ci(
            f"alpha-row{i:02d}",
            "alpha",
            git_status=GitStatus(wt="+1 -2", ahead_diff="+3 -4", ahead_count="7", conflict="ok")
            if i % 2
            else None,
        )
        for i in range(n)
    ]
    return dashboard.RepoGroup("alpha", "/repos/alpha", tmp_path / "a.yaml", containers)


def _rows_shown(lines):
    """How many container rows the table draws (the `rowNN` lines above the panel)."""
    panel = next((i for i, ln in enumerate(lines) if i and "╭" in ln), len(lines))
    return sum(1 for ln in lines[:panel] if re.search(r"row\d\d", ln))


def test_table_window_does_not_depend_on_the_highlighted_row(tmp_path):
    group = _mixed_group(tmp_path)
    shown = set()
    for selected in (
        dashboard.Row("repo", "alpha"),
        dashboard.Row("container", "alpha-row10"),
        dashboard.Row("container", "alpha-row11"),
        dashboard.Row("container", "alpha-row12"),
    ):
        lines = _frame([group], selected, height=30, width=80)
        assert len(lines) <= 30 and lines[-1].startswith("╰")
        shown.add((_rows_shown(lines), next(i for i, ln in enumerate(lines) if i and "╭" in ln)))
    assert len(shown) == 1, shown


@pytest.mark.parametrize("height", [8, 10, 12, 14])
def test_cursor_row_is_visible_and_the_frame_fits_at_small_heights(tmp_path, height):
    for row in ("alpha-row00", "alpha-row21", "alpha-row39"):
        lines = _frame(
            [_mixed_group(tmp_path)], dashboard.Row("container", row), height=height, width=80
        )
        assert len(lines) <= height, (height, row)
        assert lines[-1].startswith("╰"), (height, row)
        assert row.removeprefix("alpha-") in "\n".join(lines), (height, row)


@pytest.mark.parametrize("height", [8, 10, 12])
def test_a_panel_too_small_for_two_rows_is_dropped(tmp_path, height):
    lines = _frame(
        [_mixed_group(tmp_path)], dashboard.Row("container", "alpha-row21"), height=height
    )
    assert "…" not in "\n".join(lines)
    assert not any(i and "╭" in ln for i, ln in enumerate(lines))


def test_a_menu_alone_replaces_a_panel_that_does_not_fit(tmp_path):
    menu = dashboard.MenuState("alpha-row21", [("Shell", "shell"), ("Tmux", "tmux")])
    lines = _frame(
        [_mixed_group(tmp_path)],
        dashboard.Row("container", "alpha-row21"),
        overlay=menu,
        height=10,
    )
    text = "\n".join(lines)
    assert "alpha-row21 →" in text and "network" not in text
    assert "row21" in text and lines[-1].startswith("╰") and len(lines) <= 10


def test_the_table_keeps_min_rows_with_the_panel_at_height_14(tmp_path):
    """Pins MIN_TABLE_ROWS: at 14 the panel gets what is left after the floor."""
    lines = _frame(
        [_mixed_group(tmp_path)], dashboard.Row("container", "alpha-row21"), height=14, width=80
    )
    # Literal on purpose: MIN_TABLE_ROWS (5) lines under the header, two of
    # them taken by the "more" markers.
    assert _rows_shown(lines) >= 3


def test_a_narrow_terminal_drops_the_details_beside_a_menu(tmp_path):
    menu = dashboard.MenuState("alpha-row21", [("Shell", "shell"), ("Tmux", "tmux")])
    lines = _frame(
        [_mixed_group(tmp_path)],
        dashboard.Row("container", "alpha-row21"),
        overlay=menu,
        height=30,
        width=40,
    )
    text = "\n".join(lines)
    assert "alpha-row21 →" in text and "network" not in text


def test_a_cursor_row_taller_than_the_window_is_not_cut_away(tmp_path, mocker):
    mocker.patch.object(
        dashboard,
        "container_row",
        lambda group, c, fields, widths, selected, marks: f"{c.name}\nsecond\nthird\nfourth",
    )
    group = _mixed_group(tmp_path, 12)
    fields = dashboard.visible_fields(_FRAME_NOW, group.containers)[:1]
    sections = dashboard._RepoSections(
        [group],
        fields,
        (10,),
        dashboard.Row("container", "alpha-row06"),
        frozenset(),
        empty=False,
    )
    console = Console(width=60, record=True, file=io.StringIO())
    for budget in (4, 5, 6):
        lines = console.render_lines(dataclasses.replace(sections, max_rows=budget), pad=False)
        text = "\n".join("".join(seg.text for seg in line) for line in lines)
        assert "alpha-row06" in text, budget
        assert len(lines) <= budget, budget


def test_gather_live_adds_extra_roots_to_the_registered_ones(mocker, tmp_path):
    registered = tmp_path / "reg"
    extra = tmp_path / "extra"
    mocker.patch.object(dashboard, "registered_repo_roots", return_value=[registered, extra])
    rows = mocker.patch.object(dashboard, "gather_rows", return_value=[])
    incus = mocker.Mock()

    dashboard.gather_live(incus, [extra], with_git=True)

    rows.assert_called_once_with(incus, [extra, registered], with_git=True)


def _group(prefix, root=None):
    return dashboard.RepoGroup(prefix, root, None, [])


def test_present_pins_the_cwd_group_first(tmp_path):
    groups = [_group("alpha", "/a"), _group("beta", "/b"), _group("zeta")]
    shown = dashboard.present(groups, Path("/b"))
    assert [g.prefix for g in shown] == ["beta", "alpha", "zeta"]
    assert [g.prefix for g in groups] == ["alpha", "beta", "zeta"]  # input untouched


def test_present_without_cwd_keeps_the_order():
    groups = [_group("alpha", "/a"), _group("beta", "/b")]
    assert dashboard.present(groups, None) == groups


def test_present_drops_groups_the_scope_excludes():
    from jailbee.remote_ssh.repo_scope import RemoteRepoScope

    groups = [_group("alpha", "/a"), _group("secret", "/s"), _group("gamma")]
    shown = dashboard.present(groups, None, RemoteRepoScope(frozenset({"secret"})))
    assert [g.prefix for g in shown] == ["alpha", "gamma"]


def _data_line(frame: RenderableType, marker: str) -> str:
    return next(line for line in _render_text(frame).splitlines() if marker in line)


def test_live_cpu_value_growing_does_not_shift_later_columns(tmp_path):
    """CPU 9% → 100% stays inside the column's reserve: the columns after
    it keep their positions between refreshes."""

    def frame(percent: float) -> RenderableType:
        from jailbee.procstat import ProcessActivity

        c = dataclasses.replace(
            _ci("alpha-one", "alpha"),
            cpu_percent=percent,
            cpu_limit="16",
            activity=(ProcessActivity(comm="pytest", percent=percent, count=1),),
        )
        return dashboard.render(
            [dashboard.RepoGroup("alpha", str(tmp_path), None, [c])],
            selected=None,
            now=datetime(2026, 6, 8, tzinfo=UTC),
            git_enabled=False,
            enabled=("name", "cpu", "doing"),
        )

    low, high = _data_line(frame(9), "pytest"), _data_line(frame(100), "pytest")
    assert "9%·16" in low and "100%·16" in high
    assert low.index("pytest") == high.index("pytest")


def test_overlong_doing_value_is_cut_with_an_ellipsis_on_one_line(tmp_path):
    from jailbee.procstat import ProcessActivity

    long_name = "x" * 60
    c = dataclasses.replace(
        _ci("alpha-one", "alpha"),
        activity=(ProcessActivity(comm=long_name, percent=50.0, count=1),),
    )
    out = _render_text(
        dashboard.render(
            [dashboard.RepoGroup("alpha", str(tmp_path), None, [c])],
            selected=None,
            now=datetime(2026, 6, 8, tzinfo=UTC),
            git_enabled=False,
            enabled=("name", "doing", "network"),
        )
    )
    row = [line for line in out.splitlines() if "●" in line and "pytest" not in line]
    assert len(row) == 1
    assert long_name not in row[0] and "…" in row[0]


def test_nonoverflow_details_height_is_stable_between_repo_and_container(tmp_path):
    group = _named_rows_group(tmp_path, 2)
    for height in (None, 40):
        panels = []
        for selected, title in (
            (dashboard.Row("repo", "alpha"), "╭─ alpha"),
            (dashboard.Row("container", "alpha-row01"), "╭─ row01"),
        ):
            lines = _frame([group], selected, height=height)
            start = next(i for i, line in enumerate(lines) if title in line)
            end = next(i for i in range(start + 1, len(lines)) if "╰" in lines[i])
            panels.append(lines[start : end + 1])
        assert len(panels[0]) == len(panels[1]) == dd.DETAILS_MAX_ROWS + 2


def test_optimize_key_is_documented_and_parsed():
    assert dashboard.parse_key(b"o") == "optimize"
    out = _render_text(
        dashboard.render(
            [], None, now=datetime(2026, 6, 8, tzinfo=UTC), git_enabled=False, overlay="help"
        )
    )
    assert "optimize" in out.lower() and "width" in out.lower()


def test_optimized_widths_retain_snapshot_until_reoptimized(tmp_path):
    now = datetime(2026, 6, 8, tzinfo=UTC)
    short = dashboard.RepoGroup("alpha", str(tmp_path), None, [_ci("alpha-one", "alpha")])
    long = dataclasses.replace(short, containers=[_ci("alpha-abcdefghijklmnop", "alpha")])
    enabled = ("name", "network")
    widths = dashboard.optimize_column_widths([short], now=now, enabled=enabled)

    def frame(group, budgets, width=200):
        return _render_text(
            dashboard.render(
                [group],
                None,
                now=now,
                git_enabled=False,
                enabled=enabled,
                column_widths=budgets,
            ),
            width=width,
        )

    before = next(
        line for line in frame(short, widths).splitlines() if "●" in line and "alpha" not in line
    )
    after = next(
        line for line in frame(long, widths).splitlines() if "●" in line and "alpha" not in line
    )
    assert before.index("●") == after.index("●")
    assert "abcdefghijklmnop" not in after and "…" in after
    saved = dict(widths)
    frame(long, widths, width=24)
    assert widths == saved
    updated = dashboard.optimize_column_widths([long], now=now, enabled=enabled)
    renewed = next(
        line for line in frame(long, updated).splitlines() if "●" in line and "alpha" not in line
    )
    assert "abcdefghijklmnop" in renewed
    assert renewed.index("●") > after.index("●")


def test_nonempty_columns_hide_placeholders_without_changing_preferences(tmp_path):
    now = datetime(2026, 6, 8, tzinfo=UTC)
    enabled = ("name", "pr", "job", "network", "created")
    group = dashboard.RepoGroup("alpha", str(tmp_path), None, [_ci("alpha-one", "alpha")])
    out = _render_text(dashboard.render([group], None, now=now, git_enabled=False, enabled=enabled))
    header = next(line for line in out.splitlines() if "NAME" in line)
    assert "PR" not in header and "JOB" not in header and "AGE" not in header
    assert "●" in out
    assert enabled == ("name", "pr", "job", "network", "created")


def test_run_nonempty_snapshot_survives_refresh_until_optimize(mocker, tmp_path):
    group = dashboard.RepoGroup("alpha", str(tmp_path), None, [_ci("alpha-one", "alpha")])
    frames = mocker.patch.object(dashboard, "render", wraps=dashboard.render)
    _mock_terminal(mocker)
    mocker.patch.object(
        dashboard, "seed_view_state", return_value=dashboard.ViewState(columns=("name", "pr"))
    )
    _fake_state(mocker, [group])
    mocker.patch.object(dashboard.select, "select", return_value=([True], [], []))
    keys = iter([b"r", b"o", b"\x03"])

    def read(fd, size):
        group.containers[0].pr_number = 42
        return next(keys)

    mocker.patch.object(dashboard.os, "read", side_effect=read)
    dashboard.run(mocker.Mock(), None)
    shown = [call.kwargs["shown_columns"] for call in frames.call_args_list]
    assert shown[0] == shown[1] == ("name",)
    assert "pr" in shown[2]


def test_run_space_unfold_restores_nonempty_columns_without_reoptimizing(mocker, tmp_path):
    group = dashboard.RepoGroup("alpha", str(tmp_path), None, [_ci("alpha-one", "alpha")])
    frames = mocker.patch.object(dashboard, "render", wraps=dashboard.render)
    _drive_run(
        mocker,
        [b"o", b" "],
        [group],
        view_state=dashboard.ViewState(
            columns=("name", "state", "network"), folded=frozenset({"alpha"})
        ),
    )
    initial, optimized, unfolded = frames.call_args_list[:3]
    assert initial.kwargs["shown_columns"] == optimized.kwargs["shown_columns"] == ("name",)
    assert unfolded.kwargs["shown_columns"] == ("name", "state", "network")
    assert unfolded.kwargs["column_widths"] == optimized.kwargs["column_widths"]
    output = _render_text(dashboard.render(*unfolded.args, **unfolded.kwargs))
    assert "ST" in output and "NET" in output
    assert "●" in output


def test_optimized_widths_are_named_without_first_column_indent(tmp_path):
    now = datetime(2026, 6, 8, tzinfo=UTC)
    group = dashboard.RepoGroup("alpha", str(tmp_path), None, [_ci("alpha-one", "alpha")])
    widths = dashboard.optimize_column_widths([group], now=now, enabled=("name", "network"))
    fields = dashboard.visible_fields(now, group.containers, ("name", "network"))
    forward = dashboard._dashboard_column_widths(fields, widths)
    reverse = dashboard._dashboard_column_widths(list(reversed(fields)), widths)
    assert forward[0] == widths[fields[0].name] + 2
    assert forward[1] == widths[fields[1].name]
    assert reverse == (forward[1] + 2, forward[0] - 2)


def test_dashboard_formatting_contracts():
    from rich.text import Text

    from jailbee.dashboard_format import dashboard_header
    from jailbee.lifecycle import ContainerInfo, ls_field_specs
    from jailbee.qtui.model import card_content

    def container(**overrides):
        values = {
            "name": "p-foo",
            "state": "Running",
            "network": "strict",
            "ip": None,
            "memory_limit": "4 GiB",
            "repo": "p",
        }
        values.update(overrides)
        return ContainerInfo(**values)

    now = datetime(2026, 10, 7, 12, tzinfo=UTC)
    state = next(
        f for f in dashboard.visible_fields(now, [container()], ["state"]) if f.name == "state"
    )
    assert state.cell(container()) == "▶"
    assert state.cell(container(state="Stopped")) == "■"
    assert state.cell(container(state="Frozen")) == "Ⅱ"
    unknown = state.cell(container(state="<custom>[Running]</custom>"))
    assert Text.from_markup(unknown).plain == "<custom>[Running]</custom>"
    assert state.json(container()) == "Running"

    network = next(
        f for f in dashboard.visible_fields(now, [container()], ["network"]) if f.name == "network"
    )
    assert network.cell(container()) == "●"
    assert network.cell(container(network="loose", loose_until=now + timedelta(seconds=45))) == "○"
    assert network.cell(container(network="loose", loose_until=now + timedelta(minutes=12))) == "○"
    assert (
        network.cell(container(network="loose", loose_until=now + timedelta(hours=3, minutes=59)))
        == "○"
    )
    assert network.cell(container(network="loose", loose_until=None)) == "○"
    assert network.cell(container(network=None)) == "-"
    assert network.json(container()) == "strict"

    age = next(
        f for f in dashboard.visible_fields(now, [container()], ["created"]) if f.name == "created"
    )
    assert age.cell(container(created_at=now - timedelta(seconds=42))) == "42s"
    assert age.cell(container(created_at=now - timedelta(minutes=3, seconds=30))) == "3m"
    assert age.cell(container(created_at=now - timedelta(hours=3, minutes=59))) == "3h"
    assert age.cell(container(created_at=now - timedelta(days=2, hours=3))) == "2d"
    assert age.cell(container(created_at=now + timedelta(days=1))) == "0s"
    assert age.cell(container(created_at=None)) == "—"
    assert age.json(container(created_at=now)) == now.isoformat()

    names = (
        "state",
        "network",
        "created",
        "full_name",
        "memory_limit",
        "loose_until",
        "agent_compact",
        "target_diff",
        "local_diff",
        "conflict",
    )
    fields = dashboard.visible_fields(now, [container()], names)
    headers = {field.name: field.header for field in fields}
    assert headers == {
        "state": "ST",
        "network": "NET",
        "created": "AGE",
        "full_name": "FULL",
        "memory_limit": "LIMIT",
        "loose_until": "UNTIL",
        "agent_compact": "AI",
        "target_diff": "DIFF",
        "local_diff": "L DIFF",
        "conflict": "MERGE",
    }
    canonical = {field.name: field.header for field in ls_field_specs(now=now, all_repos=False)}
    assert canonical["state"] == "STATE"
    assert canonical["network"] == "NETWORK"
    assert canonical["created"] == "CREATED"
    assert canonical["full_name"] == "FULL NAME"
    assert canonical["memory_limit"] == "MEMORY LIMIT"
    assert canonical["loose_until"] == "LOOSE UNTIL"
    assert canonical["agent_compact"] == "AGENT*"
    assert canonical["issues"] == "ISSUES"
    issue = next(f for f in ls_field_specs(now=now, all_repos=False) if f.name == "issues")
    assert dashboard_header(issue) == "ISS"

    mem = next(f for f in dashboard.visible_fields(now, [container()], ["mem"]) if f.name == "mem")
    memory = container(memory_usage=1_073_741_824)
    assert mem.cell(memory) == "1.0G/4 GiB"
    assert mem.json(memory) == {"usage": 1_073_741_824, "limit": "4 GiB"}

    content = card_content(
        container(), dashboard.visible_fields(now, [container()], ["name", "state"])
    )
    assert content.state == "Running"


def test_dashboard_remaining_compact_cells_preserve_canonical_data(mocker):
    from rich.text import Text

    from jailbee.git_status import GitStatus
    from jailbee.lifecycle import ls_field_specs
    from jailbee.procstat import ProcessActivity

    now = datetime(2026, 10, 7, 12, tzinfo=UTC)
    c = _ci("p-one", "p")
    c.mode = "mount"
    c.job_phase = "starting"
    c.network = "loose"
    c.base_branch = "dev[branch]"
    c.git_status = GitStatus(
        "clean", "clean", "0", "ok", local_diff="clean", target_diff="clean", base_source="tracking"
    )
    c.activity = (ProcessActivity("node x2, worker", 12.0, 2),)
    fields = {
        f.name: f
        for f in dashboard.visible_fields(
            now,
            [c],
            (
                "base",
                "mode",
                "job",
                "doing",
                "wt",
                "target_diff",
                "local_diff",
                "git_status",
                "ttl",
            ),
        )
    }
    canonical = {f.name: f for f in ls_field_specs(now=now, all_repos=False)}
    assert Text.from_markup(fields["base"].cell(c)).plain == "dev[branch] ↗"
    c.mode = "clone"
    assert fields["mode"].cell(c) == "cln"
    c.mode = "mount"
    assert fields["mode"].cell(c) == "mnt"
    assert Text.from_markup(fields["doing"].cell(c)).plain == "node x2, worker×2"  # noqa: RUF001 - intentional multiplication sign
    assert canonical["doing"].cell(c) == "node x2, worker x2"
    for name in ("wt", "target_diff", "local_diff"):
        assert Text.from_markup(fields[name].cell(c)).plain == "✓"
        assert canonical[name].json(c) == fields[name].json(c) == "clean"
    assert fields["git_status"].header == "GIT"
    mocker.patch("jailbee.background.worker_alive", return_value=True)
    c.job_pid = 123
    for phase, expected in (
        ("starting", "start"),
        ("creating", "create"),
        ("cloning", "clone"),
        ("stopping", "stop"),
        ("deleting", "delete"),
        ("destroying", "destroy"),
        ("failed", "failed"),
    ):
        c.job_phase = phase
        assert Text.from_markup(fields["job"].cell(c)).plain == expected
    c.job_kind = "autostart"
    c.job_phase = "deps[red]"
    assert Text.from_markup(fields["job"].cell(c)).plain == "auto:deps[red]"
    mocker.patch("jailbee.background.worker_alive", return_value=False)
    assert Text.from_markup(fields["job"].cell(c)).plain == "deps[red] (dead)"
    c.network = "loose"
    c.loose_until = now + timedelta(hours=3, minutes=59)
    assert Text.from_markup(fields["ttl"].cell(c)).plain == "3h59m"
