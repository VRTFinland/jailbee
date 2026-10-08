"""Tests for the gie dashboard module (pure logic; no real Incus/TTY)."""

from __future__ import annotations

import contextlib
import dataclasses
import io
import itertools
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from rich.console import Console, RenderableType

from jailbee.accounts.models import AgentActivity
from jailbee.agent_status import AgentSummary
from jailbee.config.loader import _scratch_prefix
from jailbee.dashboard import columns as dcolumns
from jailbee.dashboard import details as dd
from jailbee.dashboard import dispatch as ddispatch
from jailbee.dashboard import menus as dmenus
from jailbee.dashboard import model as dmodel
from jailbee.dashboard.tui import frame as tframe
from jailbee.dashboard.tui import keys as tkeys
from jailbee.dashboard.tui import menu_state as tmenu
from jailbee.dashboard.tui import overlay as toverlay
from jailbee.dashboard.tui import session as tsession
from jailbee.dashboard.tui import terminal as tterm
from jailbee.git_status import GitStatus
from jailbee.lifecycle import ContainerInfo
from tests.dashboard_fixtures import WIDE as _WIDE
from tests.dashboard_fixtures import autostart_ci as _autostart_ci
from tests.dashboard_fixtures import cfg_group as _cfg_group
from tests.dashboard_fixtures import ci as _ci
from tests.dashboard_fixtures import every_verb_group as _every_verb_group
from tests.dashboard_fixtures import frame_at as _frame_at
from tests.dashboard_fixtures import header as _header
from tests.dashboard_fixtures import mount_group as _mount_group
from tests.dashboard_fixtures import named_rows_group as _named_rows_group
from tests.dashboard_fixtures import repo_menu_verbs as _repo_menu_verbs
from tests.dashboard_fixtures import wide_group as _wide_group
from tests.dashboard_pilot import CREDENTIAL_GROUP_LEAF as _CREDENTIAL_GROUP_LEAF
from tests.dashboard_pilot import patch_pause

pytestmark = pytest.mark.usefixtures("no_real_branch_listing")


def test_inline_editor_keeps_shortcuts_as_text():
    state = toverlay.CommandState(text="", suggestions=(), index=0)
    assert toverlay.edit_command(state, b"q").text == "q"


def test_inline_editor_handles_editing_and_utf8():
    state = toverlay.CommandState(text="", suggestions=(), index=0)
    state = toverlay.edit_command(state, "é shell".encode())
    state = toverlay.edit_command(state, b"\x7f")
    assert state.text == "é shel"


def test_inline_editor_backspace_clears_pending_utf8_before_completed_text():
    state = toverlay.CommandState(text="a")
    state = toverlay.edit_command(state, b"\xc3")
    assert state.pending_utf8 == b"\xc3"

    state = toverlay.edit_command(state, b"\x7f")

    assert state.text == "a"
    assert state.pending_utf8 == b""


def test_inline_editor_tab_cycles_candidates():
    state = toverlay.CommandState(text="me", suggestions=("merge", "menu"), index=0)
    state = toverlay.edit_command(state, b"\t")
    assert state.text == "menu"
    assert state.index == 1


def test_inline_editor_tab_without_candidates_is_safe():
    state = toverlay.CommandState(text="merge '", suggestions=())
    assert toverlay.edit_command(state, b"\t") == state


def test_inline_editor_completion_preserves_unfinished_quote():
    from jailbee.dashboard.commands import completion_candidates

    text = "shell 'feature"
    state = toverlay.CommandState(
        text=text, suggestions=completion_candidates(text, ("feature branch",))
    )
    assert "feature branch" in state.suggestions


def test_command_binding_and_inline_render_keep_table_visible():
    group = dmodel.RepoGroup("alpha", "/alpha", None, [_ci("alpha-x", "alpha")])
    overlay = toverlay.CommandState(text="git d", suggestions=("git diff",))
    screen = Console(width=100, record=True)
    screen.print(
        tframe.render(
            [group],
            dmodel.Row("container", "alpha-x"),
            now=datetime.now(UTC),
            git_enabled=True,
            overlay=overlay,
        )
    )
    rendered = screen.export_text()
    assert tkeys.parse_key(b"!") == "command"
    assert "  x" in rendered
    assert "git d" in rendered
    assert "git diff" in rendered


def test_render_title_has_no_refresh_clock():
    group = dmodel.RepoGroup("alpha", "/alpha", None, [_ci("alpha-x", "alpha")])
    frame = _render_text(
        tframe.render(
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
    assert "scratch" not in dmodel.NOTHING_TO_SHOW


def test_nothing_to_show_message_still_offers_a_way_out():
    """Naming no cause must not also mean naming no remedy: the old wording
    carried the next step by implication, and dropping it left the user with a
    dead end. The remedy stays cause-neutral — both branches out of it work
    whichever of the causes fired."""
    assert "jailbee config init" in dmodel.NOTHING_TO_SHOW
    assert "registered repo" in dmodel.NOTHING_TO_SHOW


def test_nothing_to_show_message_also_points_at_config_validate():
    """One of the causes is a config file that exists but will not parse —
    `jailbee config init` is the wrong remedy for that (it errors rather than
    overwriting), so the message must also point at `jailbee config validate`.
    """
    assert "jailbee config validate" in dmodel.NOTHING_TO_SHOW


def test_collect_repo_roots_puts_cwd_first_and_dedupes(mocker):
    a = Path("/repos/a")
    b = Path("/repos/b")
    mocker.patch.object(dmodel, "registered_repo_roots", return_value=[a, b])
    # cwd root equals an already-registered one -> no duplicate, cwd wins order
    result = dmodel.collect_repo_roots(b)
    assert result == [b, a]


def test_collect_repo_roots_no_cwd(mocker):
    a = Path("/repos/a")
    mocker.patch.object(dmodel, "registered_repo_roots", return_value=[a])
    assert dmodel.collect_repo_roots(None) == [a]


def test_collect_repo_roots_empty(mocker):
    mocker.patch.object(dmodel, "registered_repo_roots", return_value=[])
    assert dmodel.collect_repo_roots(None) == []


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
    assert dmodel.registered_repo_roots() == [live]


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
    assert dmodel.registered_repo_roots(scope=scope) == [roots[0]]


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


def _ctx(**kw: object) -> dmenus.MenuContext:
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
    return dmenus.MenuContext(**fields)


def _apps(*verbs: str) -> list[dmodel.AppMenuEntry]:
    """``AppMenuEntry`` list where each label defaults to its own verb — the
    description-less fallback most tests don't care to distinguish from."""
    return [dmodel.AppMenuEntry(v, v) for v in verbs]


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

    mocker.patch.object(dmodel, "load_repo_config", side_effect=fake_load)
    mocker.patch.object(dmodel, "list_containers", side_effect=fake_list)

    groups = dmodel.gather_rows(mocker.MagicMock(), [other_root, cwd_root], with_git=False)
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

    mocker.patch.object(dmodel, "list_containers", return_value=[])

    groups = dmodel.gather_rows(mocker.MagicMock(), [repo], with_git=False)

    assert [g.prefix for g in groups] == [prefix]
    assert groups[0].repo_root == str(repo)
    # No file on disk -> no config path. Task 10b is what re-enables its menu.
    assert groups[0].config_path is None
    assert groups[0].containers == []
    target = dmodel.RepoTarget.of(groups[0])
    assert target is not None
    assert target.repo_root == repo


def test_gather_rows_carries_the_repos_loose_ttl_default(tmp_path, mocker, make_cfg):
    """The Qt duration dialog pre-selects this, so it must be the repo's own
    configured `loose_auto_revert.after`, not the first preset."""
    cfg = make_cfg(tmp_path / "alpha", loose_auto_revert={"after": "45m"})
    root = tmp_path / "alpha"
    mocker.patch.object(dmodel, "load_repo_config", return_value=cfg)

    def fake_list(c, incus, *, all_repos, with_git_status, with_background, instances):
        return [] if all_repos else [_ci("alpha-one", "alpha")]

    mocker.patch.object(dmodel, "list_containers", side_effect=fake_list)

    groups = dmodel.gather_rows(mocker.MagicMock(), [root], with_git=False)

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
    mocker.patch.object(dmodel, "load_repo_config", return_value=cfg)

    def fake_list(c, incus, *, all_repos, with_git_status, with_background, instances):
        return [] if all_repos else [_ci("alpha-one", "alpha")]

    mocker.patch.object(dmodel, "list_containers", side_effect=fake_list)

    groups = dmodel.gather_rows(mocker.MagicMock(), [root], with_git=False)

    assert groups[0].optional_mounts == ("aws", "gcloud")


def test_orphan_groups_have_no_optional_mounts():
    assert dmodel.RepoGroup("orphan", None, None, []).optional_mounts == ()


def test_gather_rows_loose_ttl_default_is_none_when_policy_disabled(tmp_path, mocker, make_cfg):
    """None tells the GUI not to ask: a disabled policy schedules no TTL."""
    cfg = make_cfg(tmp_path / "alpha", loose_auto_revert={"enabled": False})
    root = tmp_path / "alpha"
    mocker.patch.object(dmodel, "load_repo_config", return_value=cfg)

    def fake_list(c, incus, *, all_repos, with_git_status, with_background, instances):
        return [] if all_repos else [_ci("alpha-one", "alpha")]

    mocker.patch.object(dmodel, "list_containers", side_effect=fake_list)

    groups = dmodel.gather_rows(mocker.MagicMock(), [root], with_git=False)

    assert groups[0].loose_ttl_default is None


def test_gather_rows_carries_the_repos_agent_homes(tmp_path, mocker, make_cfg):
    from tests.conftest import with_agent

    cfg = with_agent(
        make_cfg(tmp_path / "alpha", shared_dir=tmp_path / "shared"), "claude", enabled=True
    )
    root = tmp_path / "alpha"
    mocker.patch.object(dmodel, "load_repo_config", return_value=cfg)

    def fake_list(c, incus, *, all_repos, with_git_status, with_background, instances):
        return [_ci("orphan-x", "orphan")] if all_repos else [_ci("alpha-one", "alpha")]

    mocker.patch.object(dmodel, "list_containers", side_effect=fake_list)

    groups = dmodel.gather_rows(mocker.MagicMock(), [root], with_git=False)

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
    mocker.patch.object(dmodel, "load_repo_config", return_value=cfg)

    def fake_list(c, incus, *, all_repos, with_git_status, with_background, instances):
        return [] if all_repos else [_ci("alpha-one", "alpha")]

    mocker.patch.object(dmodel, "list_containers", side_effect=fake_list)

    groups = dmodel.gather_rows(mocker.MagicMock(), [root], with_git=False)

    assert groups[0].push_action_default == "rebase"
    assert groups[0].push_source_default == "current"


def test_gather_rows_push_defaults_fall_back_to_the_config_defaults(tmp_path, mocker, make_cfg):
    """PushConfig's own defaults: 'ask' is why the GUI has a dialog at all."""
    cfg = make_cfg(tmp_path / "alpha")
    root = tmp_path / "alpha"
    mocker.patch.object(dmodel, "load_repo_config", return_value=cfg)

    def fake_list(c, incus, *, all_repos, with_git_status, with_background, instances):
        return [] if all_repos else [_ci("alpha-one", "alpha")]

    mocker.patch.object(dmodel, "list_containers", side_effect=fake_list)

    groups = dmodel.gather_rows(mocker.MagicMock(), [root], with_git=False)

    assert groups[0].push_action_default == "ask"
    assert groups[0].push_source_default == "base"


def test_gather_rows_renders_an_int_after_as_minutes(tmp_path, mocker, make_cfg):
    cfg = make_cfg(tmp_path / "alpha", loose_auto_revert={"after": 20})
    root = tmp_path / "alpha"
    mocker.patch.object(dmodel, "load_repo_config", return_value=cfg)

    def fake_list(c, incus, *, all_repos, with_git_status, with_background, instances):
        return [] if all_repos else [_ci("alpha-one", "alpha")]

    mocker.patch.object(dmodel, "list_containers", side_effect=fake_list)

    groups = dmodel.gather_rows(mocker.MagicMock(), [root], with_git=False)

    assert groups[0].loose_ttl_default == "20m"


def test_gather_rows_orphan_group_has_no_loose_ttl_default(tmp_path, mocker, make_cfg):
    cfg = make_cfg(tmp_path / "alpha")
    root = tmp_path / "alpha"
    mocker.patch.object(dmodel, "load_repo_config", return_value=cfg)

    def fake_list(c, incus, *, all_repos, with_git_status, with_background, instances):
        if all_repos:
            return [_ci("alpha-one", "alpha"), _ci("gamma-x", "gamma")]
        return [_ci("alpha-one", "alpha")]

    mocker.patch.object(dmodel, "list_containers", side_effect=fake_list)

    groups = dmodel.gather_rows(mocker.MagicMock(), [root], with_git=False)

    orphan = next(g for g in groups if g.prefix == "gamma")
    assert orphan.loose_ttl_default is None


def test_gather_rows_surfaces_orphans_view_only(tmp_path, mocker, make_cfg):
    cfg = make_cfg(tmp_path / "alpha")
    root = tmp_path / "alpha"
    mocker.patch.object(dmodel, "load_repo_config", return_value=cfg)

    def fake_list(c, incus, *, all_repos, with_git_status, with_background, instances):
        if all_repos:
            return [_ci("alpha-one", "alpha"), _ci("gamma-x", "gamma")]
        return [_ci("alpha-one", "alpha")]

    mocker.patch.object(dmodel, "list_containers", side_effect=fake_list)
    groups = dmodel.gather_rows(mocker.MagicMock(), [root], with_git=False)
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

    mocker.patch.object(dmodel, "load_repo_config", side_effect=fake_load)
    mocker.patch.object(dmodel, "list_containers", side_effect=fake_list)

    groups = dmodel.gather_rows(mocker.MagicMock(), [beta_root, alpha_root], with_git=False)
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

    mocker.patch.object(dmodel, "load_repo_config", side_effect=fake_load)
    mocker.patch.object(dmodel, "list_containers", side_effect=fake_list)

    groups = dmodel.gather_rows(mocker.MagicMock(), [empty_root, populated_root], with_git=False)
    assert [g.prefix for g in groups] == ["alpha", "beta"]
    alpha = groups[0]
    assert alpha.containers == []
    assert dmodel.RepoTarget.of(alpha) is not None


def test_gather_rows_empty_repo_roots_returns_empty(mocker):
    # No repos -> no base_cfg -> no orphan scan -> empty result, no calls.
    lc = mocker.patch.object(dmodel, "list_containers")
    incus = mocker.MagicMock()
    result = dmodel.gather_rows(incus, [], with_git=False)
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
    mocker.patch.object(dmodel, "load_repo_config", side_effect=cfgs.__getitem__)
    lc = mocker.patch.object(dmodel, "list_containers", return_value=[])
    incus = mocker.MagicMock()

    dmodel.gather_rows(incus, [alpha_root, beta_root], with_git=False)

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

    mocker.patch.object(dmodel, "load_repo_config", side_effect=fake_load)
    mocker.patch.object(dmodel, "list_containers", side_effect=fake_list)
    groups = dmodel.gather_rows(mocker.MagicMock(), [good_root, bad_root], with_git=False)
    assert [g.prefix for g in groups] == ["alpha"]


def test_view_only_note_explains_an_orphan_group():
    groups = [dmodel.RepoGroup("gamma", None, None, [_ci("gamma-x", "gamma")])]
    note = dmenus.view_only_note(groups, "gamma-x")
    assert note is not None
    assert "gamma" in note and "view-only" in note


def test_view_only_note_is_none_for_a_scratch_group():
    """A repo with no config file is actionable — its config was synthesized,
    not missing — so there is nothing to explain and nothing to disable."""
    groups = [dmodel.RepoGroup("gamma", "/gamma", None, [_ci("gamma-x", "gamma")])]

    assert dmenus.view_only_note(groups, "gamma-x") is None


def test_actions_for_container_offers_actions_to_a_scratch_group():
    """The action menu is gated on the repo being addressable, not on a config
    file existing: a scratch repo gets the same menu as a configured one."""
    groups = [dmodel.RepoGroup("gamma", "/gamma", None, [_ci("gamma-x", "gamma")])]

    verbs = [verb for _, verb in dmenus.actions_for_container(groups, "gamma-x")]

    assert "tmux" in verbs and "destroy" in verbs


def test_remote_actions_filter_against_canonical_policy_and_argv():
    from jailbee.config.models_remote import RemoteCommandPolicy, RemoteSSHConfig
    from jailbee.dashboard.commands import dashboard_action_argv

    groups = [dmodel.RepoGroup("alpha", "/alpha", None, [_ci("alpha-1", "alpha")])]
    policy = RemoteSSHConfig(commands=RemoteCommandPolicy(mode="allowlist", allow=["git merge"]))

    assert [
        verb
        for _, verb in dmenus.actions_for_container(
            groups, "alpha-1", over_ssh=True, ssh_policy=policy
        )
    ] == ["merge"]
    assert dmenus.group_menu_actions(
        dmenus.actions_for_container(groups, "alpha-1", over_ssh=True, ssh_policy=policy)
    ) == [dmenus.MenuGroup("Git →", (("Merge into…", "merge"),))]
    assert dashboard_action_argv("tmux", "alpha-1", force=True) == ["tmux", "alpha-1", "--force"]
    assert "--config" not in dashboard_action_argv("git push --pr", "alpha-1")


def test_remote_policy_filters_all_git_leaves_without_hiding_other_actions():
    from jailbee.config.models_remote import RemoteCommandPolicy, RemoteSSHConfig

    groups = [dmodel.RepoGroup("alpha", "/alpha", None, [_ci("alpha-1", "alpha")])]
    policy = RemoteSSHConfig(commands=RemoteCommandPolicy(mode="allowlist", allow=["shell"]))

    permitted = dmenus.actions_for_container(groups, "alpha-1", over_ssh=True, ssh_policy=policy)

    assert permitted == [("Open shell", "shell")]
    assert dmenus.group_menu_actions(permitted) == [("Open shell", "shell")]


def test_remote_full_and_disabled_actions_follow_policy():
    from jailbee.config.models_remote import RemoteCommandPolicy, RemoteSSHConfig

    groups = [dmodel.RepoGroup("alpha", "/alpha", None, [_ci("alpha-1", "alpha")])]
    full = RemoteSSHConfig(commands=RemoteCommandPolicy(mode="full"))
    disabled = RemoteSSHConfig(commands=RemoteCommandPolicy(mode="disabled"))

    full_verbs = [
        verb
        for _, verb in dmenus.actions_for_container(
            groups, "alpha-1", over_ssh=True, ssh_policy=full
        )
    ]
    assert {"merge", "shell", "tmux"} <= set(full_verbs)
    assert dmenus.actions_for_container(groups, "alpha-1", over_ssh=True, ssh_policy=disabled) == []
    assert (
        dmenus.group_menu_actions(
            dmenus.actions_for_container(groups, "alpha-1", over_ssh=True, ssh_policy=disabled)
        )
        == []
    )


def test_view_only_note_is_none_when_the_container_has_actions():
    groups = [
        dmodel.RepoGroup(
            "alpha", "/alpha", Path("/alpha/.jailbee/config.yaml"), [_ci("alpha-1", "alpha")]
        )
    ]
    assert dmenus.view_only_note(groups, "alpha-1") is None


def test_view_only_note_is_none_for_an_unknown_container():
    """Nothing to explain about a container that is not on screen — the
    caller must stay silent rather than pop up an empty menu."""
    assert dmenus.view_only_note([], "ghost") is None


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
    mocker.patch.object(dmodel, "registered_repo_roots", side_effect=lambda: list(registered))
    gr = mocker.patch.object(dmodel, "gather_rows", return_value=[])
    incus = mocker.MagicMock()

    dmodel.gather_live(incus, [], with_git=False)
    assert gr.call_args.args[1] == [a]

    registered.append(b)  # a `jailbee new` in repo b just registered it
    dmodel.gather_live(incus, [], with_git=True)
    assert gr.call_args.args[1] == [a, b]
    assert gr.call_args.kwargs == {"with_git": True}


def test_carry_forward_git_status_fills_in_from_previous_snapshot():
    from jailbee.git_status import GitStatus

    status = GitStatus(wt="+1 -0", ahead_diff="clean", ahead_count="1", conflict="ok")
    prev = [
        dmodel.RepoGroup(
            "a",
            "/a",
            Path("/a/.jailbee/config.yaml"),
            [_ci("a-1", "a")],
        )
    ]
    prev[0].containers[0].git_status = status

    new = [
        dmodel.RepoGroup(
            "a",
            "/a",
            Path("/a/.jailbee/config.yaml"),
            [_ci("a-1", "a")],
        )
    ]
    assert new[0].containers[0].git_status is None

    dmodel.carry_forward_git_status(new, prev)

    assert new[0].containers[0].git_status is status


def test_carry_forward_git_status_leaves_unmatched_name_none():
    from jailbee.git_status import GitStatus

    status = GitStatus(wt="+1 -0", ahead_diff="clean", ahead_count="1", conflict="ok")
    prev = [dmodel.RepoGroup("a", "/a", Path("/a/.jailbee/config.yaml"), [_ci("a-1", "a")])]
    prev[0].containers[0].git_status = status

    new = [dmodel.RepoGroup("a", "/a", Path("/a/.jailbee/config.yaml"), [_ci("a-2", "a")])]

    dmodel.carry_forward_git_status(new, prev)

    assert new[0].containers[0].git_status is None


def test_carry_forward_git_status_does_not_overwrite_existing():
    from jailbee.git_status import GitStatus

    old_status = GitStatus(wt="+1 -0", ahead_diff="clean", ahead_count="1", conflict="ok")
    new_status = GitStatus(wt="+2 -0", ahead_diff="clean", ahead_count="2", conflict="ok")
    prev = [dmodel.RepoGroup("a", "/a", Path("/a/.jailbee/config.yaml"), [_ci("a-1", "a")])]
    prev[0].containers[0].git_status = old_status

    new = [dmodel.RepoGroup("a", "/a", Path("/a/.jailbee/config.yaml"), [_ci("a-1", "a")])]
    new[0].containers[0].git_status = new_status

    dmodel.carry_forward_git_status(new, prev)

    assert new[0].containers[0].git_status is new_status


def test_carry_forward_git_status_empty_prev_is_noop():
    new = [dmodel.RepoGroup("a", "/a", Path("/a/.jailbee/config.yaml"), [_ci("a-1", "a")])]

    dmodel.carry_forward_git_status(new, [])

    assert new[0].containers[0].git_status is None


def test_selectable_rows_interleaves_headers_and_containers():
    """Repo headers are selectable rows. That is what lets `Enter` reach a
    group whose containers are hidden — and it makes the cursor behave like
    the tree it is drawing."""
    groups = [
        dmodel.RepoGroup("a", "/a", None, [_ci("a-1", "a"), _ci("a-2", "a")]),
        dmodel.RepoGroup("b", "/b", None, [_ci("b-1", "b")]),
    ]
    rows = dmodel.selectable_rows(groups)
    assert rows == [
        dmodel.Row("repo", "a"),
        dmodel.Row("container", "a-1"),
        dmodel.Row("container", "a-2"),
        dmodel.Row("repo", "b"),
        dmodel.Row("container", "b-1"),
    ]


def test_selectable_rows_skips_a_folded_groups_containers():
    """A folded group keeps its header — that is how you unfold it — and
    contributes none of its containers. Its neighbours are untouched."""
    groups = [
        dmodel.RepoGroup("a", "/a", None, [_ci("a-1", "a")]),
        dmodel.RepoGroup("b", "/b", None, [_ci("b-1", "b")]),
    ]
    assert dmodel.selectable_rows(groups, frozenset({"a"})) == [
        dmodel.Row("repo", "a"),
        dmodel.Row("repo", "b"),
        dmodel.Row("container", "b-1"),
    ]


def test_selectable_rows_includes_an_empty_group():
    groups = [dmodel.RepoGroup("a", "/a", None, [])]
    assert dmodel.selectable_rows(groups) == [dmodel.Row("repo", "a")]
    assert dmenus.new_container_target(groups, dmodel.Row("repo", "a")) is groups[0]


def test_move_selection_clamps_at_edges():
    rows = [dmodel.Row("repo", "x"), dmodel.Row("container", "x-1")]
    assert dmodel.move_selection(rows, None, 1) == rows[0]
    assert dmodel.move_selection(rows, rows[0], -1) == rows[0]  # clamp at top
    assert dmodel.move_selection(rows, rows[1], 1) == rows[1]  # clamp at bottom
    assert dmodel.move_selection(rows, rows[0], 1) == rows[1]
    assert dmodel.move_selection([], rows[0], 1) is None


def test_reconcile_selection_keeps_or_clamps():
    a, b = dmodel.Row("container", "a"), dmodel.Row("container", "b")
    assert dmodel.reconcile_selection([a, b], b, 0) == b
    assert dmodel.reconcile_selection([a], b, 1) == a
    assert dmodel.reconcile_selection([], b, 0) is None
    assert dmodel.reconcile_selection([a, b], None, 0) == a


def test_container_of_narrows_a_header_row_to_none():
    """The action path takes a container name. A header row has none, so it
    falls into the existing 'nothing selected' notice rather than needing new
    gating at every call site."""
    assert dmodel.container_of(dmodel.Row("container", "a-1")) == "a-1"
    assert dmodel.container_of(dmodel.Row("repo", "a")) is None
    assert dmodel.container_of(None) is None


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
    actions = dmenus.menu_actions(_ctx())
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
    eligible = dmenus.MenuContext(state="Running", has_repo=True, mode="clone")
    stopped = dmenus.MenuContext(state="Stopped", has_repo=True, mode="clone")
    mounted = dmenus.MenuContext(state="Running", has_repo=True, mode="mount")
    orphan = dmenus.MenuContext(state="Running", has_repo=False, mode="clone")
    assert ("Merge into…", "merge") in dmenus.menu_actions(eligible)
    for context in (stopped, mounted, orphan):
        assert "merge" not in [verb for _, verb in dmenus.menu_actions(context)]


def test_menu_actions_running_ide_enabled_only():
    actions = dmenus.menu_actions(_ctx(apps=_apps("ide")))
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
    actions = dmenus.menu_actions(_ctx(apps=_apps("chrome")))
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
    actions = dmenus.menu_actions(_ctx(apps=_apps("ide", "chrome")))
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
    actions = dmenus.menu_actions(_ctx(apps=_apps("firefox", "figma")))
    verbs = [verb for _label, verb in actions]
    assert "firefox" in verbs
    assert "figma" in verbs
    assert ("Launch firefox", "firefox") in actions
    assert ("Launch figma", "figma") in actions
    assert verbs.index("firefox") < verbs.index("figma") < verbs.index("pr")


def test_remote_action_menu_offers_no_app_launches():
    """A GUI app would open on the host's display, not the SSH client's."""
    apps = _apps("ide", "chrome", "figma")
    local = dmenus.menu_actions(_ctx(apps=apps))
    remote = dmenus.menu_actions(_ctx(apps=apps, remote=True))

    assert {verb for _label, verb in local} >= {"ide", "chrome", "figma"}
    assert not {verb for _label, verb in remote} & {"ide", "chrome", "figma"}
    assert [a for a in local if a[1] not in {"ide", "chrome", "figma"}] == remote
    assert [
        item.label
        for item in dmenus.group_menu_actions(remote)
        if isinstance(item, dmenus.MenuGroup)
    ] == ["PR →", "Git →"]
    assert ("Egress…", "net egress ls") in remote


def test_remote_quick_keys_refuse_gui_apps_and_say_why():
    group = dmodel.RepoGroup("alpha", "/repos/alpha", None, [_ci("alpha-x", "alpha")])
    group.apps = _apps("ide", "chrome")

    assert tkeys.quick_verb([group], "alpha-x", "action:ide") == "ide"
    assert tkeys.quick_verb([group], "alpha-x", "action:ide", remote=True) is None
    assert tkeys.quick_verb([group], "alpha-x", "action:chrome", remote=True) is None
    assert tkeys.quick_verb([group], "alpha-x", "action:shell", remote=True) == "shell"
    note = tkeys.quick_reject_note([group], "alpha-x", "action:ide", remote=True)
    assert note == "GUI apps are not available over remote SSH"
    assert tmenu.open_menu([group], "alpha-x", remote=True) is not None


def test_action_menu_renders_a_builtins_description_as_its_label():
    """A builtin's `AppSpec.description` (e.g. "JetBrains idea") is real,
    user-facing English — the bare verb the earlier lowercase labels used is
    not what the registry actually carries."""
    actions = dmenus.menu_actions(_ctx(apps=[dmodel.AppMenuEntry("ide", "JetBrains idea")]))
    assert ("Launch JetBrains idea", "ide") in actions


def test_action_menu_falls_back_to_the_verb_when_description_is_empty():
    """A user's `apps:` entry that never set `description` must still render
    something readable, not a blank label."""
    actions = dmenus.menu_actions(_ctx(apps=[dmodel.AppMenuEntry("figma", "figma")]))
    assert ("Launch figma", "figma") in actions


def test_action_menu_has_no_apps_when_none_are_configured():
    actions = dmenus.menu_actions(_ctx(apps=[]))
    verbs = [verb for _label, verb in actions]
    assert "chrome" not in verbs and "ide" not in verbs


def test_menu_actions_stopped():
    actions = dmenus.menu_actions(_ctx(state="Stopped"))
    assert [a for _, a in actions] == ["start", "net egress ls", "destroy"]


def test_menu_actions_orphan_disabled():
    assert dmenus.menu_actions(_ctx(has_repo=False)) == []


def test_menu_actions_orphan_disabled_regardless_of_flags():
    assert dmenus.menu_actions(_ctx(has_repo=False, apps=_apps("ide", "chrome"))) == []


def test_menu_actions_unknown_state_only_destroy():
    assert [a for _, a in dmenus.menu_actions(_ctx(state="Frozen"))] == ["destroy"]


def test_menu_actions_running_network_strict_offers_loose():
    verbs = [a for _, a in dmenus.menu_actions(_ctx(current_network="strict"))]
    assert "net loose" in verbs
    assert "net strict" not in verbs


def test_menu_actions_running_network_loose_offers_strict():
    verbs = [a for _, a in dmenus.menu_actions(_ctx(current_network="loose"))]
    assert "net strict" in verbs
    assert "net loose" not in verbs


def test_menu_actions_running_network_unknown_offers_both():
    verbs = [a for _, a in dmenus.menu_actions(_ctx(current_network=None))]
    assert "net strict" in verbs
    assert "net loose" in verbs


def test_menu_actions_stopped_has_no_network_entries():
    verbs = [a for _, a in dmenus.menu_actions(_ctx(state="Stopped"))]
    assert verbs.count("net egress ls") == 1
    assert not any(v in {"net strict", "net loose"} for v in verbs)


def test_network_group_keeps_mode_eligibility_and_stopped_egress_view():
    running = dmenus.group_menu_actions(dmenus.menu_actions(_ctx()), include_network=True)
    network = next(
        item for item in running if isinstance(item, dmenus.MenuGroup) and item.label == "Network →"
    )
    assert [verb for _, verb in network.actions] == ["net loose", "net egress ls"]
    stopped = dmenus.group_menu_actions(
        dmenus.menu_actions(_ctx(state="Stopped")), include_network=True
    )
    network = next(
        item for item in stopped if isinstance(item, dmenus.MenuGroup) and item.label == "Network →"
    )
    assert network.actions == (("Egress…", "net egress ls"),)
    assert dmenus.group_menu_actions(dmenus.menu_actions(_ctx(has_repo=False))) == []


def test_egress_view_remote_policy_is_independent():
    from jailbee.config.models_remote import RemoteCommandPolicy, RemoteSSHConfig

    group = dmodel.RepoGroup("alpha", "/alpha", None, [_ci("alpha-1", "alpha")])
    view_only = RemoteSSHConfig(
        commands=RemoteCommandPolicy(mode="allowlist", allow=["net egress ls"])
    )
    actions = dmenus.actions_for_container([group], "alpha-1", over_ssh=True, ssh_policy=view_only)
    assert [(label, verb) for label, verb in actions if verb.startswith("net ")] == [
        ("Egress…", "net egress ls")
    ]
    restricted_full = RemoteSSHConfig(commands=RemoteCommandPolicy(mode="full"), restrict_host=True)
    actions = dmenus.actions_for_container(
        [group], "alpha-1", over_ssh=True, ssh_policy=restricted_full
    )
    assert "net egress ls" in [verb for _, verb in actions]
    assert "net egress add" not in [verb for _, verb in actions]
    assert "net egress rm" not in [verb for _, verb in actions]


def test_repo_menu_offers_egress_only_for_actionable_repo():
    groups = [
        dmodel.RepoGroup("alpha", "/alpha", None, []),
        dmodel.RepoGroup("orphan", None, None, [_ci("orphan-1", "orphan")]),
    ]
    actionable = tmenu.open_repo_menu(groups, "alpha", frozenset())
    assert actionable is not None
    assert dmenus.MenuGroup("Network →", (("Egress…", "net egress ls"),)) in actionable.actions
    orphan = tmenu.open_repo_menu(groups, "orphan", frozenset())
    assert orphan is not None
    assert all(not isinstance(item, dmenus.MenuGroup) for item in orphan.actions)


def test_repo_menu_egress_respects_ssh_read_permission():
    from jailbee.config.models_remote import RemoteCommandPolicy, RemoteSSHConfig

    group = dmodel.RepoGroup("alpha", "/alpha", None, [])
    allowed = RemoteSSHConfig(
        commands=RemoteCommandPolicy(mode="allowlist", allow=["net egress ls"])
    )
    denied = RemoteSSHConfig(commands=RemoteCommandPolicy(mode="disabled"))
    allowed_menu = tmenu.open_repo_menu(
        [group], "alpha", frozenset(), ssh_policy=allowed, over_ssh=True
    )
    denied_menu = tmenu.open_repo_menu(
        [group], "alpha", frozenset(), ssh_policy=denied, over_ssh=True
    )
    assert allowed_menu is not None and any(
        isinstance(item, dmenus.MenuGroup) and item.label == "Network →"
        for item in allowed_menu.actions
    )
    assert denied_menu is not None and all(
        not isinstance(item, dmenus.MenuGroup) for item in denied_menu.actions
    )


def test_repo_network_menu_is_a_submenu_and_escape_returns_to_parent():
    group = dmodel.RepoGroup("alpha", "/alpha", None, [])
    menu = tmenu.open_repo_menu([group], "alpha", frozenset())
    assert menu is not None
    assert menu.actions[4] == dmenus.MenuGroup("Network →", (("Egress…", "net egress ls"),))

    menu.index = 4
    child, verb = tmenu.enter_menu(menu)
    assert verb is None
    assert isinstance(child, tmenu.RepoMenuState)
    assert child.active_group == "Network →"
    assert tmenu.menu_verb(child) == "net egress ls"
    parent = tmenu.back_menu(child)
    assert parent is not None
    assert parent.active_group is None
    assert parent.index == 4
    assert tmenu.menu_verb(parent) is None


def test_repo_network_submenu_remains_gated_by_ssh_read_permission():
    from jailbee.config.models_remote import RemoteCommandPolicy, RemoteSSHConfig

    group = dmodel.RepoGroup("alpha", "/alpha", None, [])
    denied = RemoteSSHConfig(commands=RemoteCommandPolicy(mode="disabled"))
    menu = tmenu.open_repo_menu([group], "alpha", frozenset(), ssh_policy=denied, over_ssh=True)
    assert menu is not None
    assert all(not isinstance(item, dmenus.MenuGroup) for item in menu.actions)


@pytest.mark.parametrize("network", [False, True])
def test_ssh_egress_permission_by_scope_and_network_switch(network: bool) -> None:
    from jailbee.config.models_remote import RemoteCommandPolicy, RemoteSSHConfig
    from jailbee.dashboard.commands import permitted
    from jailbee.dashboard.egress import EgressState, egress_argv

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

    group = dmodel.RepoGroup("alpha", "/alpha", None, [_ci("alpha-1", "alpha")])
    local = [verb for _, verb in dmenus.actions_for_container([group], "alpha-1")]
    assert "net loose" in local
    policy = RemoteSSHConfig(commands=RemoteCommandPolicy(mode="full"), network=network)
    verbs = [
        verb
        for _, verb in dmenus.actions_for_container(
            [group], "alpha-1", over_ssh=True, ssh_policy=policy
        )
    ]
    assert ("net loose" in verbs) is network


def test_menu_actions_orphan_disabled_even_with_network():
    assert dmenus.menu_actions(_ctx(has_repo=False, current_network="strict")) == []


def test_menu_actions_network_entries_ordered_after_chrome_before_restart():
    actions = dmenus.menu_actions(_ctx(apps=_apps("ide", "chrome")))
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
    actions = dmenus.menu_actions(_ctx(pr_number=123))
    assert [verb for _, verb in actions[:5]] == [
        "tmux",
        "shell",
        "outbox browse",
        "pr --open",
        "pr",
    ]


def test_menu_actions_stopped_includes_open_pr_when_pr_known():
    actions = dmenus.menu_actions(_ctx(state="Stopped", pr_number=7))
    assert actions == [
        ("Start", "start"),
        ("Open PR", "pr --open"),
        ("Egress…", "net egress ls"),
        ("Destroy", "destroy"),
    ]
    assert dmenus.group_menu_actions(actions, include_network=True) == [
        ("Start", "start"),
        dmenus.MenuGroup("PR →", (("Open PR", "pr --open"),)),
        dmenus.MenuGroup("Network →", (("Egress…", "net egress ls"),)),
        ("Destroy", "destroy"),
    ]


def test_menu_actions_omits_open_pr_when_no_pr():
    running = dmenus.menu_actions(_ctx())
    stopped = dmenus.menu_actions(_ctx(state="Stopped"))
    assert ("Open PR", "pr --open") not in running
    assert ("Open PR", "pr --open") not in stopped


def test_menu_actions_orphan_stays_empty_even_with_pr():
    assert dmenus.menu_actions(_ctx(has_repo=False, pr_number=123)) == []


def test_menu_actions_running_offers_the_workflow_verbs():
    """Sessions lead, then PR and Git; unknown Git status retains its leaves."""
    verbs = [v for _, v in dmenus.menu_actions(_ctx())]
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
    grouped = dmenus.group_menu_actions(leaves)
    assert [item.label if isinstance(item, dmenus.MenuGroup) else item[0] for item in grouped] == [
        "Attach tmux",
        "PR →",
        "Git →",
    ]
    assert grouped[1] == dmenus.MenuGroup("PR →", tuple(leaves[1:4]))
    assert grouped[2] == dmenus.MenuGroup("Git →", tuple(leaves[4:]))
    assert dmenus.group_menu_actions([]) == []


def test_group_menu_actions_collects_registry_launches_at_first_occurrence():
    leaves = [
        ("Attach tmux", "tmux"),
        ("Launch Chrome", "chrome"),
        ("Open shell", "shell"),
        ("Launch Figma", "apps run figma --container"),
        ("Create/update PR", "pr"),
        ("Show diff", "git diff"),
    ]
    assert dmenus.group_menu_actions(leaves) == [
        leaves[0],
        dmenus.MenuGroup("Launch →", (leaves[1], leaves[3])),
        leaves[2],
        dmenus.MenuGroup("PR →", (leaves[4],)),
        dmenus.MenuGroup("Git →", (leaves[5],)),
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
    assert dmenus.group_menu_actions(leaves) == [
        dmenus.MenuGroup("Git →", (leaves[0], leaves[4])),
        dmenus.MenuGroup("Launch →", (leaves[1],)),
        leaves[2],
        dmenus.MenuGroup("PR →", (leaves[3], leaves[5])),
    ]


def test_menu_actions_mount_mode_keeps_outbox_without_git():
    grouped = dmenus.group_menu_actions(
        dmenus.menu_actions(_ctx(mode="mount")), include_network=True
    )
    assert ("Outbox", "outbox browse") in grouped
    assert [item.label for item in grouped if isinstance(item, dmenus.MenuGroup)] == ["Network →"]


def test_grouped_git_leaves_respect_known_clean_and_unknown_status():
    clean = _dirty(wt="clean", ahead_diff="clean", ahead_count="0")
    unknown = _dirty(wt="?", ahead_diff="?", ahead_count="?")
    for status, expected in (
        (clean, ["merge", "git push", "git retarget"]),
        (unknown, ["merge", "git pull", "git push", "git retarget", "git diff"]),
        (None, ["merge", "git pull", "git push", "git retarget", "git diff"]),
    ):
        grouped = dmenus.group_menu_actions(dmenus.menu_actions(_ctx(git_status=status)))
        git_group = next(
            item for item in grouped if isinstance(item, dmenus.MenuGroup) and item.label == "Git →"
        )
        assert [verb for _, verb in git_group.actions] == expected


def test_menu_actions_workflow_labels_name_their_verb():
    labels = {verb: label for label, verb in dmenus.menu_actions(_ctx())}
    assert labels["pr"] == "Create/update PR"
    assert labels["git push"] == "Update from base (git push)"
    assert labels["git pull"] == "Send commits to host (git pull)"
    assert labels["git diff"] == "Show diff (git diff)"
    assert labels["git retarget"] == "Change base branch (git retarget)"
    assert ddispatch.dispatch_style("git retarget") == "output"


def test_menu_actions_offers_pr_refresh_on_a_review_container():
    """A container built from someone else's PR can pull in commits the author
    pushed since, so the entry sits right after the base-update it mirrors."""
    actions = dmenus.menu_actions(_ctx(pr_number=123))
    verbs = [v for _, v in actions]
    assert verbs[verbs.index("git push") + 1] == "git push --pr"
    labels = {verb: label for label, verb in actions}
    assert labels["git push --pr"] == "Refresh from PR head (git push --pr)"


def test_menu_actions_omits_pr_refresh_on_an_authored_pr():
    """`pr_author` means jailbee opened the PR from this container's branch, so
    its head is downstream of the container and a refresh is a no-op."""
    actions = dmenus.menu_actions(_ctx(pr_number=123, pr_author=True))
    verbs = [v for _, v in actions]
    assert "git push --pr" not in verbs
    assert "pr --open" in verbs  # the PR itself is still reachable


def test_menu_actions_omits_pr_refresh_without_a_pr():
    assert "git push --pr" not in [v for _, v in dmenus.menu_actions(_ctx())]


def test_menu_actions_omits_pr_refresh_when_the_bridge_is_impossible():
    """No clone to push into: `jailbee git push` would fail in
    `sync.assert_container_publishable` on either of these."""
    for ctx in (_ctx(state="Stopped", pr_number=5), _ctx(mode="mount", pr_number=5)):
        assert "git push --pr" not in [v for _, v in dmenus.menu_actions(ctx)]


@pytest.mark.parametrize("count", [None, 2])
@pytest.mark.parametrize("mode", ["clone", "mount"])
def test_menu_has_one_outbox_unless_both_outboxes_are_empty(count, mode):
    actions = dmenus.menu_actions(
        _ctx(mode=mode, git_status=_dirty(pending_pr_actions=count, pending_issue_actions=count))
    )
    assert [v for _, v in actions].count("outbox browse") == 1
    assert not {"review apply", "issue apply"} & {v for _, v in actions}
    for ctx in (_ctx(state="Stopped", mode=mode), _ctx(has_repo=False, mode=mode)):
        assert "outbox browse" not in {v for _, v in dmenus.menu_actions(ctx)}


@pytest.mark.parametrize("mode", ["clone", "mount"])
def test_an_empty_outbox_is_not_offered(mode):
    empty = _dirty(pending_pr_actions=0, pending_issue_actions=0)
    actions = dmenus.menu_actions(_ctx(mode=mode, git_status=empty))
    assert "outbox browse" not in {v for _, v in actions}
    assert actions[:2] == [("Attach tmux", "tmux"), ("Open shell", "shell")]


@pytest.mark.parametrize(("pr", "issue"), [(0, None), (None, 0)])
def test_one_unknown_outbox_count_still_offers_the_outbox(pr, issue):
    status = _dirty(pending_pr_actions=pr, pending_issue_actions=issue)
    assert ("Outbox", "outbox browse") in dmenus.menu_actions(_ctx(git_status=status))


@pytest.mark.parametrize("git_status", [None, _dirty()])
def test_outbox_follows_the_shell_when_the_count_is_unknown(git_status):
    actions = dmenus.menu_actions(_ctx(git_status=git_status))
    assert actions[:3] == [
        ("Attach tmux", "tmux"),
        ("Open shell", "shell"),
        ("Outbox", "outbox browse"),
    ]
    menu = tmenu.MenuState("alpha-x", actions)
    assert tmenu._menu_entries(menu)[0] == ("Attach tmux", "tmux")


@pytest.mark.parametrize(("pr", "issue", "total"), [(2, None, 2), (None, 1, 1), (2, 1, 3)])
def test_pending_outbox_leads_the_menu_with_its_count(pr, issue, total):
    actions = dmenus.menu_actions(
        _ctx(git_status=_dirty(pending_pr_actions=pr, pending_issue_actions=issue))
    )
    lead = (f"Outbox ({total} pending)", "outbox browse")
    assert actions[:3] == [lead, ("Attach tmux", "tmux"), ("Open shell", "shell")]
    menu = tmenu.MenuState("alpha-x", actions)
    assert tmenu._menu_entries(menu)[0] == lead


def test_outbox_dispatch_uses_target_config_and_no_pause(mocker, tmp_path):
    run = mocker.patch.object(tsession.subprocess, "run")
    run.return_value.returncode = 0
    pause = patch_pause(mocker)
    ddispatch._dispatch_action(_dispatch_target(tmp_path), "outbox browse", "alpha-x")
    run.assert_called_once_with(
        ["jailbee", "outbox", "browse", "alpha-x", "--config", str(tmp_path / "config.yaml")],
        check=False,
        cwd=tmp_path,
    )
    pause.assert_not_called()


def test_pr_refresh_is_dispatched_as_a_printing_verb():
    """PRINTING_VERBS is matched exactly, not by leading token — without its
    own entry the refresh would lose its output in both front-ends."""
    assert "git push --pr" in dmenus.PRINTING_VERBS
    assert ddispatch.dispatch_style("git push --pr") == "output"


def test_menu_actions_mount_mode_has_no_workflow_verbs():
    """A mount-mode container has no clone of its own, so every one of these
    would fail in `sync.assert_container_publishable`."""
    verbs = [v for _, v in dmenus.menu_actions(_ctx(mode="mount"))]
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
    verbs = [v for _, v in dmenus.menu_actions(_ctx(state="Stopped"))]
    assert verbs == ["start", "net egress ls", "destroy"]


def test_menu_actions_hides_git_pull_when_nothing_is_ahead():
    verbs = [v for _, v in dmenus.menu_actions(_ctx(git_status=_dirty(ahead_count="0")))]
    assert "git pull" not in verbs
    assert "git diff" in verbs  # the working tree is still dirty
    assert "git push" in verbs  # "is the host ahead?" is not knowable here


def test_menu_actions_hides_git_diff_when_there_is_nothing_to_show():
    clean = _dirty(wt="clean", ahead_diff="clean", ahead_count="0")
    verbs = [v for _, v in dmenus.menu_actions(_ctx(git_status=clean))]
    assert "git diff" not in verbs
    assert "git pull" not in verbs


def test_menu_actions_shows_git_verbs_when_the_status_is_unknown():
    """`--no-git`, a base-tier refresh, or a failed probe must not silently
    remove actions — only a known no-op hides one."""
    unknown = _dirty(wt="?", ahead_diff="?", ahead_count="?")
    for status in (None, unknown):
        verbs = [v for _, v in dmenus.menu_actions(_ctx(git_status=status))]
        assert "git pull" in verbs
        assert "git diff" in verbs


def test_menu_actions_job_log_only_when_there_is_a_job():
    assert "job log" not in [v for _, v in dmenus.menu_actions(_ctx())]
    finished = dmenus.menu_actions(_ctx(has_job=True))
    assert ("Job log", "job log") in finished
    live = dmenus.menu_actions(_ctx(has_job=True, job_running=True))
    assert ("Job log", "job log --follow") in live


def test_menu_actions_job_log_precedes_the_pr_entries():
    """Sessions first, then diagnostics before PR and Git leaves."""
    verbs = [v for _, v in dmenus.menu_actions(_ctx(job_clearable=True, has_job=True, pr_number=7))]
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
        dmenus.menu_actions(_ctx(has_repo=False, has_job=True, pr_number=7, git_status=_dirty()))
        == []
    )


def test_open_menu_captures_the_actions_with_the_cursor_at_the_top(tmp_path):
    config_path = tmp_path / "config.yaml"
    group = dmodel.RepoGroup("alpha", str(tmp_path), config_path, [_ci("alpha-x", "alpha")])

    menu = tmenu.open_menu([group], "alpha-x")

    assert menu is not None
    assert menu.container == "alpha-x"
    assert menu.index == 0
    # the shared (Qt too) action list, plus the terminal-only entries
    assert [a for a in menu.actions if a[1] not in dmenus.TERMINAL_MENU_VERBS] == (
        dmenus.actions_for_container([group], "alpha-x")
    )
    assert ("Attach tmux", "tmux") in menu.actions


def test_open_menu_is_none_for_a_view_only_group():
    """A config-less (orphan) group has no actions, so there is no menu to open.

    The caller shows `view_only_note` instead — an empty menu panel would be
    indistinguishable from a broken one.
    """
    group = dmodel.RepoGroup("gamma", None, None, [_ci("gamma-x", "gamma")])

    assert tmenu.open_menu([group], "gamma-x") is None


def test_open_menu_is_none_for_an_unknown_or_unset_container(tmp_path):
    group = dmodel.RepoGroup(
        "alpha", str(tmp_path), tmp_path / "config.yaml", [_ci("alpha-x", "alpha")]
    )

    assert tmenu.open_menu([group], "alpha-nope") is None
    assert tmenu.open_menu([group], None) is None


def test_move_menu_clamps_at_both_edges():
    menu = tmenu.MenuState("alpha-x", [("A", "a"), ("B", "b"), ("C", "c")], index=0)

    assert tmenu.move_menu(menu, -1).index == 0  # already at the top
    assert tmenu.move_menu(menu, 1).index == 1
    assert tmenu.move_menu(tmenu.move_menu(menu, 1), 1).index == 2
    assert tmenu.move_menu(tmenu.MenuState("alpha-x", [], index=0), 1).index == 0


def test_move_menu_returns_a_new_state_and_leaves_the_original_alone():
    menu = tmenu.MenuState("alpha-x", [("A", "a"), ("B", "b")], index=0)

    moved = tmenu.move_menu(menu, 1)

    assert moved is not menu
    assert menu.index == 0


def test_menu_verb_returns_the_highlighted_verb():
    menu = tmenu.MenuState("alpha-x", [("A", "a"), ("B", "b")], index=1)

    assert tmenu.menu_verb(menu) == "b"
    assert tmenu.menu_verb(tmenu.MenuState("alpha-x", [], index=0)) is None


def _grouped_menu():
    return tmenu.MenuState(
        "alpha-x",
        [("Attach tmux", "tmux"), ("Create/update PR", "pr"), ("Show diff (git diff)", "git diff")],
    )


def test_menu_enters_groups_and_returns_to_saved_root_cursor():
    root = _grouped_menu()
    assert tmenu.back_menu(root) is None
    assert tmenu.menu_verb(tmenu.move_menu(root, 1)) is None
    assert tmenu.move_menu(root, -1).index == 0

    # Terminal order: Attach tmux, Git →, PR →.
    pr, verb = tmenu.enter_menu(tmenu.move_menu(tmenu.move_menu(root, 1), 1))
    assert verb is None
    assert pr.active_group == "PR →" and pr.index == 0 and pr.parent_index == 2
    assert tmenu.menu_verb(pr) == "pr"
    assert tmenu.enter_menu(pr) == (pr, "pr")
    assert tmenu.move_menu(pr, 1).index == 0

    parent = tmenu.back_menu(pr)
    assert parent is not None
    assert parent.active_group is None and parent.index == 2
    git, verb = tmenu.enter_menu(tmenu.move_menu(parent, -1))
    assert verb is None
    assert git.active_group == "Git →" and git.index == 0
    assert tmenu.enter_menu(git) == (git, "git diff")
    assert tmenu.back_menu(git).index == 1
    assert root.index == 0 and root.active_group is None


def test_menu_launch_submenu_navigates_and_dispatches_original_verbs():
    root = tmenu.MenuState(
        "alpha-x",
        [
            ("Attach tmux", "tmux"),
            ("Launch JetBrains idea", "ide"),
            ("Launch Figma", "apps run figma --container"),
            ("Destroy", "destroy"),
        ],
    )
    selected = tmenu.move_menu(root, 1)
    assert tmenu.menu_verb(selected) is None

    launch, verb = tmenu.enter_menu(selected)
    assert verb is None and launch.active_group == "Launch →"
    assert tmenu.menu_verb(launch) == "ide"
    assert tmenu.enter_menu(tmenu.move_menu(launch, 1))[1] == "apps run figma --container"
    parent = tmenu.back_menu(launch)
    assert parent is not None and parent.active_group is None and parent.index == 1


def test_menu_group_cursor_clamps_within_visible_entries():
    root = _grouped_menu()
    assert tmenu.move_menu(root, 10).index == 2
    git, _ = tmenu.enter_menu(tmenu.move_menu(root, 2))
    assert tmenu.move_menu(git, 10).index == 0
    assert tmenu.move_menu(git, -10).index == 0


def _hotkeys(menu: tmenu.MenuState | tmenu.RepoMenuState) -> dict[str, str | None]:
    entries = tmenu._menu_entries(menu)
    labels = [item.label if isinstance(item, dmenus.MenuGroup) else item[0] for item in entries]
    return dict(zip(labels, tmenu.menu_hotkeys(entries), strict=True))


def test_menu_hotkeys_give_running_root_entries_their_mnemonics():
    ctx = _ctx(pr_number=7, has_job=True, job_clearable=True)
    menu = tmenu.MenuState("alpha-x", dmenus.menu_actions(ctx))

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
    root = tmenu.MenuState("alpha-x", dmenus.menu_actions(ctx))

    def submenu(label: str) -> tmenu.MenuState:
        index = next(
            i
            for i, item in enumerate(tmenu._menu_entries(root))
            if isinstance(item, dmenus.MenuGroup) and item.label == label
        )
        child, _ = tmenu.enter_menu(dataclasses.replace(root, index=index))
        assert isinstance(child, tmenu.MenuState)
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
    menu = tmenu.MenuState("alpha-x", dmenus.menu_actions(_ctx(state="Stopped")))

    assert _hotkeys(menu) == {"Start": "s", "Network →": "w", "Destroy": "D"}


def test_menu_hotkeys_cover_the_repo_menu():
    menu = tmenu.RepoMenuState(
        "alpha",
        [
            ("New container…", "new"),
            ("New from PR…", "new-pr"),
            ("Credential group…", "credential-group"),
            ("Accounts…", "accounts"),
            dmenus.MenuGroup("Network →", (("Egress…", "net egress ls"),)),
            ("Apply config…", "apply"),
            dmenus.MenuGroup("Diagnostics →", (("Doctor", "doctor"), ("Disk usage", "disk-usage"))),
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


def _levels(menu: tmenu.MenuState | tmenu.RepoMenuState):
    """Every level of ``menu`` as (group label or None, entries)."""
    root = tmenu._menu_entries(menu)
    yield None, root
    for item in root:
        if isinstance(item, dmenus.MenuGroup):
            yield item.label, item.actions


def _assert_every_key_is_fixed(menu: tmenu.MenuState | tmenu.RepoMenuState) -> None:
    for group, entries in _levels(menu):
        if group == "Launch →":
            continue  # app labels come from the repo's config: the one dynamic level
        for item, key in zip(entries, tmenu.menu_hotkeys(entries), strict=True):
            preferred = tmenu._preferred_menu_key(item)
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
        ("Autostart status", tsession.dact.AUTOSTART_STATUS),
        ("Cancel autostart…", tsession.dact.AUTOSTART_CANCEL),
    )
    before = (
        ("Snapshots…", tsession.dact.SNAPSHOTS),
        ("Mount…", tsession.dact.MOUNT_ADD),
        ("Unmount…", tsession.dact.MOUNT_REMOVE),
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
            for a in dmenus.menu_actions(ctx)
            if a[1] not in dmenus._CONTAINER_LIFECYCLE_VERBS or a[1] in lifecycle
        ]
        if extras:
            actions = dmenus._insert_after_job(actions, autostart)
            actions = dmenus._insert_before_network(actions, before)
        actions = dmenus._with_credential_group(actions)
        _assert_every_key_is_fixed(tmenu.MenuState("alpha-x", actions))


@pytest.mark.parametrize("drop", [None, "credential-group", "accounts", "apply", "prune"])
def test_repo_menu_keys_never_move_whatever_the_policy_hides(drop):
    actions: list[dmenus.MenuItem] = [
        ("New container…", "new"),
        ("New from PR…", "new-pr"),
        ("Credential group…", "credential-group"),
        ("Accounts…", "accounts"),
        dmenus.MenuGroup("Network →", (("Egress…", "net egress ls"),)),
        ("Apply config…", tsession.dact.REPO_APPLY),
        dmenus.MenuGroup(
            tsession.dact.DIAGNOSTICS_LABEL,
            (
                ("Doctor", tsession.dact.REPO_DOCTOR),
                ("Disk usage", tsession.dact.REPO_DISK_USAGE),
            ),
        ),
        ("Prune stale containers…", tsession.dact.REPO_PRUNE),
        ("Fold", "fold"),
    ]
    menu = tmenu.RepoMenuState(
        "alpha", [a for a in actions if isinstance(a, dmenus.MenuGroup) or a[1] != drop]
    )
    _assert_every_key_is_fixed(menu)


def test_menu_hotkeys_fall_back_to_a_free_label_letter_then_digits():
    entries = [
        ("Attach tmux", "tmux"),
        ("Tally", "unknown-1"),  # preferred-free: t is taken, a is next
        ("Hook", "unknown-2"),  # h is the help key: never assigned, o follows
        ("ttt", "unknown-3"),  # every letter taken: the first digit
    ]

    assert tmenu.menu_hotkeys(entries) == ["t", "a", "o", "1"]


def test_menu_hotkeys_never_take_a_key_the_open_menu_already_handles():
    reserved = {"j", "k", "q", "h", "?", "S"}
    entries = [(f"{ch}{ch.upper()}", f"x-{ch}") for ch in "jkqhs"]

    keys = tmenu.menu_hotkeys(entries)

    assert not reserved & {k for k in keys if k}
    assert len({k for k in keys if k}) == len([k for k in keys if k])


def test_menu_hotkeys_preferred_keys_win_over_earlier_fallbacks():
    # "Tally" sits first, but `t` is Attach tmux's own key: the fallback yields.
    entries = [("Tally", "unknown"), ("Attach tmux", "tmux")]

    assert tmenu.menu_hotkeys(entries) == ["a", "t"]


def test_hotkey_menu_moves_the_cursor_to_the_entry_or_returns_none():
    root = _grouped_menu()  # Attach tmux, Git →, PR →

    hit = tmenu.hotkey_menu(root, b"p")
    assert hit is not None and hit.index == 2 and hit.active_group is None
    assert tmenu.hotkey_menu(root, b"z") is None
    assert tmenu.hotkey_menu(root, b"\x1b[A") is None


def test_render_menu_shows_each_entry_with_its_key():
    console = Console(width=60, record=True)
    console.print(tframe._render_menu(_grouped_menu()))
    text = console.export_text()

    assert "[t] Attach tmux" in text
    assert "[g] Git →" in text
    assert "[p] PR →" in text


# --- RepoTarget: how a spawned `jailbee` child is pointed at one repo --------


def _dispatch_target(tmp_path, name="config.yaml"):
    """A configured repo rooted at ``tmp_path``, as the dispatch paths see it."""
    return dmodel.RepoTarget(tmp_path, tmp_path / name)


def test_repo_target_uses_config_flag_when_there_is_a_file(tmp_path):
    t = dmodel.RepoTarget(repo_root=tmp_path, config_path=tmp_path / ".jailbee" / "config.yaml")

    assert t.flags() == ["--config", str(tmp_path / ".jailbee" / "config.yaml")]
    assert t.cwd() == tmp_path


def test_repo_target_falls_back_to_cwd_for_a_scratch_repo(tmp_path):
    """There is no path to point `--config` at; the child resolves its config
    from the directory it runs in."""
    t = dmodel.RepoTarget(repo_root=tmp_path, config_path=None)

    assert t.flags() == []
    assert t.cwd() == tmp_path


def test_repo_target_of_reads_the_groups_root_and_config(tmp_path):
    group = dmodel.RepoGroup("alpha", str(tmp_path), tmp_path / "c.yaml", [])

    assert dmodel.RepoTarget.of(group) == _dispatch_target(tmp_path, "c.yaml")


def test_repo_target_of_accepts_a_scratch_group(tmp_path):
    """A repo with no config file is still a real repo with a real root, so it
    is addressable — that is the whole point of the cwd fallback."""
    target = dmodel.RepoTarget.of(dmodel.RepoGroup("alpha", str(tmp_path), None, []))

    assert target is not None
    assert target.flags() == []
    assert target.cwd() == tmp_path


def test_repo_target_of_is_none_for_an_orphan_group():
    """An orphan group has no repo root, so there is nothing to address at
    all: neither a `--config` path nor a directory to run the child in."""
    assert dmodel.RepoTarget.of(dmodel.RepoGroup("gamma", None, None, [])) is None


def test_dispatch_action_runs_jailbee_with_the_repos_config(mocker, tmp_path):
    config_path = tmp_path / "config.yaml"
    run = mocker.patch.object(tsession.subprocess, "run")
    run.return_value.returncode = 0

    rc = ddispatch._dispatch_action(_dispatch_target(tmp_path), "net loose", "alpha-x")

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
    run = mocker.patch.object(tsession.subprocess, "run")
    run.return_value.returncode = 0

    for verb in ("tmux", "shell", "ide", "chrome"):
        run.reset_mock()
        ddispatch._dispatch_action(_dispatch_target(tmp_path), verb, "alpha-x")
        run.assert_called_once_with(
            ["jailbee", verb, "alpha-x", "--config", str(config_path), "--force"],
            check=False,
            cwd=tmp_path,
        )


def test_dispatch_action_rechecks_remote_policy_before_subprocess(mocker, tmp_path):
    from jailbee.config.models_remote import RemoteCommandPolicy, RemoteSSHConfig
    from jailbee.remote_ssh.router import RouteError

    run = mocker.patch.object(tsession.subprocess, "run")
    policy = RemoteSSHConfig(commands=RemoteCommandPolicy(mode="allowlist", allow=["git merge"]))
    with pytest.raises(RouteError):
        ddispatch._dispatch_action(
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
    run = mocker.patch.object(tsession.subprocess, "run")
    run.return_value.returncode = 0

    ddispatch._dispatch_action(_dispatch_target(tmp_path), "restart", "alpha-x")

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
    verb = dmodel._app_menu_verb(spec)

    config_path = tmp_path / "config.yaml"
    run = mocker.patch.object(tsession.subprocess, "run")
    run.return_value.returncode = 0

    ddispatch._dispatch_action(_dispatch_target(tmp_path), verb, "alpha-x")

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

    verb = dmodel._app_menu_verb(spec)
    assert verb == "apps run figma --container"

    config_path = tmp_path / "config.yaml"
    run = mocker.patch.object(tsession.subprocess, "run")
    run.return_value.returncode = 0

    ddispatch._dispatch_action(_dispatch_target(tmp_path), verb, "alpha-x")

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
    assert {"shell", "tmux", "ide", "chrome", "firefox", "browser"} == dmenus.ATTACH_VERBS


def test_dispatch_action_reports_the_commands_exit_code(mocker, tmp_path):
    run = mocker.patch.object(tsession.subprocess, "run")
    run.return_value.returncode = 2

    assert ddispatch._dispatch_action(_dispatch_target(tmp_path), "tmux", "alpha-x") == 2


def test_dispatch_action_omits_the_config_flag_for_a_scratch_repo(mocker, tmp_path):
    """A scratch repo has no path to point `--config` at. It is addressed by
    running the child in the repo root instead, so the cwd is the only thing
    that says which repo this is — it must actually be set."""
    run = mocker.patch.object(tsession.subprocess, "run")
    run.return_value.returncode = 0

    ddispatch._dispatch_action(dmodel.RepoTarget(tmp_path, None), "net loose", "alpha-x")

    run.assert_called_once_with(["jailbee", "net", "loose", "alpha-x"], check=False, cwd=tmp_path)


def test_dispatch_action_pages_a_scratch_repo_from_its_repo_root(mocker, tmp_path):
    """The pager path spawns the command itself, so it needs the cwd too —
    otherwise `jailbee git diff` in a scratch repo resolves the dashboard's own
    directory instead of the row's."""
    mocker.patch.object(ddispatch, "pager_argv", return_value=["less", "-R"])
    popen = mocker.patch.object(tsession.subprocess, "Popen")
    popen.return_value.wait.return_value = 0
    # Patching `Popen` alone is process-wide and takes `subprocess.run` with
    # it too — mocked here (as the sibling non-scratch test already does) so
    # a regression that reaches the plain `run` fallback fails with a
    # readable assertion instead of a `TypeError` from the real subprocess API.
    run = mocker.patch.object(tsession.subprocess, "run")

    ddispatch._dispatch_action(dmodel.RepoTarget(tmp_path, None), "git diff", "alpha-x")

    producer = popen.call_args_list[0]
    assert producer.args[0] == ["jailbee", "git", "diff", "alpha-x", "--color"]
    assert producer.kwargs["cwd"] == tmp_path
    run.assert_not_called()  # the paged path replaces the plain run entirely


def test_dispatch_style_classifies_every_menu_verb():
    """`git diff` is long enough to want a pager; the other printing verbs get
    a keypress pause, because Live repaints over their output on return."""
    assert ddispatch.dispatch_style("git diff") == "paged"
    for verb in ("pr", "git push", "git pull", "job log", "job log --follow"):
        assert ddispatch.dispatch_style(verb) == "output", verb
    for verb in ("tmux", "shell", "ide", "chrome", "net loose", "restart", "destroy"):
        assert ddispatch.dispatch_style(verb) == "plain", verb


def test_dispatch_style_leaves_pr_open_alone():
    """`pr --open` only opens a browser — pausing on it would be noise, and it
    is why the classification is exact rather than by leading token."""
    assert ddispatch.dispatch_style("pr --open") == "plain"


def test_inline_noninteractive_commands_pause_for_output():
    assert ddispatch.command_needs_pause("job ls")
    assert ddispatch.command_needs_pause("ls")
    assert not ddispatch.command_needs_pause("shell")


def test_every_printing_verb_is_a_real_menu_verb():
    """Guards against a typo in PRINTING_VERBS: a classified verb the menu never
    offers would silently never take its own code path."""
    offered = set()
    for state in ("Running", "Stopped", "Frozen"):
        offered |= {
            verb
            for _label, verb in dmenus.menu_actions(
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
            for _label, verb in dmenus.menu_actions(
                _ctx(state=state, has_job=True, job_running=True)
            )
        }
    assert dmenus.PRINTING_VERBS <= offered, f"unknown verbs: {dmenus.PRINTING_VERBS - offered}"
    # The paged verbs are a subset, so the split cannot drop or invent one.
    assert ddispatch._PAGED_VERBS <= dmenus.PRINTING_VERBS
    assert ddispatch._OUTPUT_VERBS | ddispatch._PAGED_VERBS == dmenus.PRINTING_VERBS


def test_pager_argv_prefers_the_environment(mocker):
    mocker.patch.dict(ddispatch.os.environ, {"PAGER": "bat -p"}, clear=False)
    assert ddispatch.pager_argv() == ["bat", "-p"]


def test_pager_argv_falls_back_to_less_then_more(mocker):
    mocker.patch.dict(ddispatch.os.environ, {}, clear=True)
    which = mocker.patch.object(ddispatch.shutil, "which", return_value=None)
    assert ddispatch.pager_argv() is None

    which.side_effect = lambda n: "/usr/bin/more" if n == "more" else None
    assert ddispatch.pager_argv() == ["more"]

    which.side_effect = lambda n: f"/usr/bin/{n}"
    assert ddispatch.pager_argv() == ["less", "-R"]


def test_dispatch_action_pages_the_diff_and_forces_colour(mocker, tmp_path):
    config_path = tmp_path / "config.yaml"
    mocker.patch.object(ddispatch, "pager_argv", return_value=["less", "-R"])
    popen = mocker.patch.object(tsession.subprocess, "Popen")
    popen.return_value.wait.return_value = 0
    run = mocker.patch.object(tsession.subprocess, "run")

    rc = ddispatch._dispatch_action(_dispatch_target(tmp_path), "git diff", "alpha-x")

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
    mocker.patch.object(ddispatch, "pager_argv", return_value=["less", "-R"])
    popen = mocker.patch.object(tsession.subprocess, "Popen")
    run = mocker.patch.object(tsession.subprocess, "run")
    run.return_value.returncode = 0
    wait = patch_pause(mocker)

    rc = ddispatch._dispatch_action(_dispatch_target(tmp_path), "git diff", "alpha-x", remote=True)

    assert rc == 0
    popen.assert_not_called()
    assert run.call_args.args[0][:4] == ["jailbee", "git", "diff", "alpha-x"]
    wait.assert_called_once_with()


def test_dispatch_action_pauses_after_a_printing_verb(mocker, tmp_path):
    run = mocker.patch.object(tsession.subprocess, "run")
    run.return_value.returncode = 0
    wait = patch_pause(mocker)

    ddispatch._dispatch_action(_dispatch_target(tmp_path), "git push", "alpha-x")

    wait.assert_called_once_with()


def test_dispatch_action_pauses_after_merge(mocker, tmp_path):
    run = mocker.patch.object(tsession.subprocess, "run")
    run.return_value.returncode = 0
    wait = patch_pause(mocker)

    ddispatch._dispatch_action(_dispatch_target(tmp_path), "merge", "alpha-x")

    assert run.call_args.args[0][:3] == ["jailbee", "merge", "alpha-x"]
    wait.assert_called_once_with()


def test_dispatch_action_does_not_pause_after_an_interactive_verb(mocker, tmp_path):
    """tmux and shell end when the user leaves them; there is nothing left to
    read, and an extra keypress would just be in the way."""
    run = mocker.patch.object(tsession.subprocess, "run")
    run.return_value.returncode = 0
    wait = patch_pause(mocker)

    ddispatch._dispatch_action(_dispatch_target(tmp_path), "tmux", "alpha-x")

    wait.assert_not_called()


def test_dispatch_action_falls_back_to_a_pause_when_there_is_no_pager(mocker, tmp_path):
    mocker.patch.object(ddispatch, "pager_argv", return_value=None)
    run = mocker.patch.object(tsession.subprocess, "run")
    run.return_value.returncode = 3
    wait = patch_pause(mocker)

    rc = ddispatch._dispatch_action(_dispatch_target(tmp_path), "git diff", "alpha-x")

    assert rc == 3
    wait.assert_called_once_with()
    assert "--color" not in run.call_args.args[0]  # no pager, so no forced colour


def test_dispatch_action_falls_back_when_the_pager_cannot_be_spawned(mocker, tmp_path):
    """`which` said yes and `exec` said no. The command still has to run, and
    its output still has to be readable."""
    mocker.patch.object(ddispatch, "pager_argv", return_value=["less", "-R"])
    producer = mocker.MagicMock()
    popen = mocker.patch.object(tsession.subprocess, "Popen")
    popen.side_effect = [producer, OSError("no less")]
    run = mocker.patch.object(tsession.subprocess, "run")
    run.return_value.returncode = 0
    wait = patch_pause(mocker)

    rc = ddispatch._dispatch_action(_dispatch_target(tmp_path), "git diff", "alpha-x")

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
    mocker.patch.object(ddispatch, "pager_argv", return_value=["less", "-R"])
    popen = mocker.patch.object(tsession.subprocess, "Popen")
    popen.side_effect = OSError("no such directory")
    run = mocker.patch.object(tsession.subprocess, "run")

    with pytest.raises(OSError):
        ddispatch._dispatch_action(_dispatch_target(tmp_path), "git diff", "alpha-x")

    run.assert_not_called()  # must not fall back to a doomed retry


def test_dispatch_falls_back_to_a_pager_unavailable_when_the_pager_itself_fails(mocker, tmp_path):
    """Sanity check alongside the test above: the pager-missing case is still
    a `_PagerUnavailableError` (an `OSError` subclass), and `_run_paged` raising it
    is what the call site's `except _PagerUnavailableError` actually catches."""
    mocker.patch.object(ddispatch, "pager_argv", return_value=["less", "-R"])
    producer = mocker.MagicMock()
    popen = mocker.patch.object(tsession.subprocess, "Popen")
    popen.side_effect = [producer, OSError("no less")]

    with pytest.raises(ddispatch._PagerUnavailableError):
        ddispatch._run_paged(["jailbee", "git", "diff"], ["less", "-R"], tmp_path)


def test_actions_for_container_matches_menu_actions():
    from pathlib import Path

    from jailbee.dashboard.menus import actions_for_container, menu_actions
    from jailbee.dashboard.model import RepoGroup
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
    mocker.patch.object(dmodel, "load_repo_config", return_value=cfg)

    def fake_list(c, incus, *, all_repos, with_git_status, with_background, instances):
        return [] if all_repos else [_ci("alpha-one", "alpha")]

    mocker.patch.object(dmodel, "list_containers", side_effect=fake_list)
    groups = dmodel.gather_rows(mocker.MagicMock(), [root], with_git=False)
    group = next(g for g in groups if g.prefix == "alpha")
    assert group.apps == [
        dmodel.AppMenuEntry("ide", "JetBrains idea"),
        dmodel.AppMenuEntry("apps run figma --container", "figma"),
    ]


def test_gather_rows_orphan_groups_have_no_apps(tmp_path, mocker, make_cfg):
    cfg = make_cfg(tmp_path / "alpha")
    root = tmp_path / "alpha"
    mocker.patch.object(dmodel, "load_repo_config", return_value=cfg)

    def fake_list(c, incus, *, all_repos, with_git_status, with_background, instances):
        if all_repos:
            return [_ci("alpha-one", "alpha"), _ci("gamma-x", "gamma")]
        return [_ci("alpha-one", "alpha")]

    mocker.patch.object(dmodel, "list_containers", side_effect=fake_list)
    groups = dmodel.gather_rows(mocker.MagicMock(), [root], with_git=False)
    orphan = next(g for g in groups if g.prefix == "gamma")
    assert orphan.apps == []


def test_visible_fields_excludes_hidden_and_respects_default_table():
    from datetime import datetime

    c = ContainerInfo(
        name="p-foo", state="Running", network="strict", ip=None, memory_limit="2GB", repo="p"
    )
    names = [f.name for f in dcolumns.visible_fields(datetime.now().astimezone(), [c])]

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

    dashboard_names = [f.name for f in dcolumns.visible_fields(now, [c])]
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
    fields = dcolumns.visible_fields(now, [loose, strict])
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
    network_field = next(f for f in dcolumns.visible_fields(now, [loose]) if f.name == "network")
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
    fields = dcolumns.visible_fields(now, [c])
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
    names = [f.name for f in dcolumns.visible_fields(now, [with_pr])]
    assert "pr" in names


def test_rendered_columns_do_not_follow_conditional_cell_presence():
    now = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)
    plain = dmodel.RepoGroup("alpha", "/a", None, [_ci("alpha-x", "alpha")])
    with_pr = dataclasses.replace(
        plain,
        containers=[dataclasses.replace(plain.containers[0], pr_number=42)],
    )
    for width in (56, 80, 120):
        rendered = [
            _render_text(
                tframe.render(
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
    plain = dmodel.RepoGroup("alpha", "/a", None, [_ci("alpha-x", "alpha")])
    active = dataclasses.replace(
        plain,
        containers=[dataclasses.replace(plain.containers[0], name="alpha-" + "x" * 36)],
    )
    for width in (36, 56, 80, 120):
        rendered = [
            _render_text(
                tframe.render(
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
        group = dmodel.RepoGroup(
            "alpha", "/a", None, [dataclasses.replace(container, base_branch=base)]
        )
        frames.append(
            _render_text(
                tframe.render(
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
    names = [f.name for f in dcolumns.visible_fields(now, [no_pr])]
    assert "pr" not in names


def test_visible_fields_defaults_to_todays_hidden_set():
    """Omitting `columns` must render exactly what the dashboard renders now."""
    from jailbee.config import DASHBOARD_DEFAULT_HIDE
    from jailbee.lifecycle import ContainerInfo

    c = ContainerInfo(name="p-foo", state="Running", network="strict", ip=None, memory_limit=None)
    names = [f.name for f in dcolumns.visible_fields(datetime.now().astimezone(), [c])]

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

    names = [f.name for f in dcolumns.visible_fields(now, [c], ["name", "state"])]
    assert names == ["name", "state"]


def test_visible_fields_enabled_set_can_add_an_off_by_default_column():
    """...and it can add one that is off by default everywhere, which a `hide`
    list never could."""
    from datetime import UTC, datetime

    c = ContainerInfo(
        name="p-foo", state="Running", network="strict", ip=None, memory_limit="2GB", repo="p"
    )
    now = datetime(2026, 6, 8, 12, 0, tzinfo=UTC)

    names = [f.name for f in dcolumns.visible_fields(now, [c], ["name", "memory_limit"])]
    assert names == ["name", "memory_limit"]


def test_visible_fields_renders_in_canonical_order_not_stored_order():
    """Stored order is not significant: the dashboards iterate the field-spec
    list and filter by membership. Column reordering is a separate feature,
    and this keeps a stored list from half-implementing it."""
    from datetime import UTC, datetime

    c = ContainerInfo(name="p-foo", state="Running", network="strict", ip=None, memory_limit=None)
    now = datetime(2026, 6, 8, 12, 0, tzinfo=UTC)

    names = [f.name for f in dcolumns.visible_fields(now, [c], ["state", "name"])]
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

    names = [f.name for f in dcolumns.visible_fields(now, [no_pr], ["name", "pr"])]
    assert names == ["name"]

    with_pr = ContainerInfo(
        name="p-bar", state="Running", network="strict", ip=None, memory_limit=None, pr_number=7
    )
    names = [f.name for f in dcolumns.visible_fields(now, [with_pr], ["name", "pr"])]
    assert names == ["name", "pr"]


def test_visible_fields_unknown_enabled_name_is_ignored():
    """A name that is no longer a real column (a removed field, a hand-edited
    row) is skipped rather than raising — same principle as the tolerant
    decode in db/view_prefs."""
    from datetime import UTC, datetime

    c = ContainerInfo(name="p-foo", state="Running", network="strict", ip=None, memory_limit=None)
    now = datetime(2026, 6, 8, 12, 0, tzinfo=UTC)

    names = [f.name for f in dcolumns.visible_fields(now, [c], ["name", "gone", "state"])]
    assert names == ["name", "state"]


def test_default_columns_matches_the_built_in_dashboard_set():
    from jailbee.config import DASHBOARD_DEFAULT_HIDE

    names = dcolumns.default_columns()
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

    names = dcolumns.enabled_from_column_config(ColumnConfig(hide=["mem", "state"]))
    assert "mem" not in names
    assert "state" not in names
    assert "name" in names
    # `hide` replaced the built-in list rather than extending it, so a column
    # the default hid is back — the legacy semantics, preserved by the seed.
    assert "created" in names


def test_enabled_from_column_config_reproduces_a_legacy_fields_block():
    from jailbee.config import ColumnConfig

    names = dcolumns.enabled_from_column_config(ColumnConfig(fields=["name", "created"]))
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

    fields = dcolumns.visible_fields(now, [loose], ["name", "network"])
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

    gcfg = dmodel.global_config_or_defaults()

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
    mocker.patch.object(dmodel, "load_global_config", return_value=(gcfg, []))

    state = dcolumns.seed_view_state(engine, FRONTEND_TUI)

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
    mocker.patch.object(dmodel, "load_global_config", return_value=(gcfg, []))

    state = dcolumns.seed_view_state(engine, FRONTEND_TUI)

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
    gcfg = GlobalConfig(dashboard=dcolumns.ColumnConfig(fields=["name", "state"]))
    mocker.patch.object(dmodel, "load_global_config", return_value=(gcfg, []))
    repo_cfg = mocker.Mock(dashboard=dcolumns.ColumnConfig(fields=["ip", "mem"]))
    load_repo_config = mocker.patch.object(dmodel, "load_repo_config", return_value=repo_cfg)

    state = dcolumns.seed_view_state(engine, FRONTEND_TUI)

    assert state.columns == ("name", "state")  # the global block's answer, not the repo's
    load_repo_config.assert_not_called()  # the repo layer is never even read


def test_seed_view_state_seeds_the_two_frontends_independently(mocker):
    from sqlmodel import SQLModel, create_engine

    from jailbee.db.view_prefs import FRONTEND_QT, FRONTEND_TUI, ViewState, save_view_state
    from jailbee.global_config import GlobalConfig

    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    gcfg = GlobalConfig(dashboard={"fields": ["name", "state"]})
    mocker.patch.object(dmodel, "load_global_config", return_value=(gcfg, []))
    save_view_state(engine, FRONTEND_TUI, ViewState(columns=("name",)))

    assert dcolumns.seed_view_state(engine, FRONTEND_TUI).columns == ("name",)
    assert dcolumns.seed_view_state(engine, FRONTEND_QT).columns == ("name", "state")


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
    mocker.patch.object(dmodel, "load_global_config", return_value=(GlobalConfig(), []))

    state = dcolumns.seed_view_state(engine, FRONTEND_TUI)

    assert state.columns == ("name",)


def test_seed_view_state_migrates_retired_diff_with_visible_notice(mocker):
    from sqlmodel import SQLModel, create_engine

    from jailbee.db.view_prefs import FRONTEND_TUI, ViewState, save_view_state

    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    save_view_state(engine, FRONTEND_TUI, ViewState(columns=("name", "ahead_diff")))
    notice = dcolumns.stored_column_migration_notice(("name", "ahead_diff"))
    assert notice is not None and "ahead_diff" in notice and "target_diff" in notice
    assert "--incoming" in notice
    shown: list[str] = []
    state = dcolumns.seed_view_state(engine, FRONTEND_TUI, on_migration=shown.append)
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
    dcolumns.seed_view_state(engine, FRONTEND_QT, on_migration=first.append)
    stored = load_view_state(engine, FRONTEND_QT)
    assert stored.columns == ("name", "target_diff")
    assert stored.folded == folded

    second: list[str] = []
    state = dcolumns.seed_view_state(engine, FRONTEND_QT, on_migration=second.append)
    assert len(first) == 1
    assert second == []
    assert state.columns == ("name", "target_diff")


def test_dashboard_config_migration_notice_is_visible_not_debug_only(mocker):
    mocker.patch.object(
        dmodel,
        "load_global_config",
        return_value=(
            dmodel.GlobalConfig(),
            ["dashboard.fields: 'ahead_diff' retired; use 'target_diff'"],
        ),
    )
    notice = dmodel.dashboard_config_migration_notice()
    assert notice is not None and "ahead_diff" in notice and "target_diff" in notice


def test_dashboard_repo_migration_notice_uses_gathered_config_without_new_reads():
    group = dmodel.RepoGroup(
        "p", "/repo", None, [], column_notice="ls.fields: 'ahead_diff' retired; use 'target_diff'"
    )
    notices = dmodel.dashboard_group_notices([group])
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
    mocker.patch.object(dmodel, "load_global_config", return_value=(GlobalConfig(), []))

    state = dcolumns.seed_view_state(engine, FRONTEND_TUI)

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
    mocker.patch.object(dmodel, "load_global_config", return_value=(GlobalConfig(), []))

    state = dcolumns.seed_view_state(engine, FRONTEND_TUI)

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
    mocker.patch.object(dmodel, "load_global_config", return_value=(GlobalConfig(), []))

    state = dcolumns.seed_view_state(engine, FRONTEND_TUI)

    assert state.columns == dcolumns.default_columns()


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
    mocker.patch.object(dmodel, "load_global_config", return_value=(GlobalConfig(), []))

    dcolumns.seed_view_state(engine, FRONTEND_TUI)

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
        console.print(f"[{tframe.CURSOR_STYLE}]x[/]", end="")
    sgr = cap.get().split("x", 1)[0]
    return [ln for ln in lines if sgr in ln]


def _screen_lines(groups, overlay, height: int) -> list[str]:
    """One frame as the full-screen dashboard draws it at ``height`` rows."""
    frame = tframe.render(
        groups,
        dmodel.Row("container", groups[0].containers[0].name),
        now=datetime(2026, 6, 8, 12, 0, tzinfo=UTC),
        git_enabled=True,
        overlay=overlay,
        height=height,
    )
    return _render_text(frame, width=100).splitlines()


def _tall_group(tmp_path, n: int) -> dmodel.RepoGroup:
    return dmodel.RepoGroup(
        "alpha", "/repos/alpha", tmp_path / "a.yaml", [_ci(f"alpha-{i}", "alpha") for i in range(n)]
    )


def test_render_scrolls_a_menu_taller_than_the_screen_to_its_cursor(tmp_path):
    menu = tmenu.MenuState("alpha-0", [(f"Action {i}", f"v{i}") for i in range(30)], index=25)
    lines = _screen_lines([_tall_group(tmp_path, 3)], menu, height=20)
    text = "\n".join(lines)
    assert len(lines) <= 20
    assert re.search(r"▸ (\[\w\]|   ) Action 25\b", text)  # keys run out before 25
    assert "Action 0 " not in text
    assert "more" in text
    assert lines[-1].startswith("╰")  # the frame's bottom border is on screen


def test_render_scrolls_a_picker_taller_than_the_screen_to_its_cursor(tmp_path):
    entries = tuple(tsession.PickerEntry(f"Entry {i}", str(i)) for i in range(30))
    picker = tsession.Picker("x", "Pick one", entries, index=29)
    lines = _screen_lines([_tall_group(tmp_path, 3)], picker, height=20)
    assert len(lines) <= 20
    assert "▸ Entry 29" in "\n".join(lines)


def test_render_cuts_a_table_taller_than_the_screen_to_keep_the_menu_visible(tmp_path):
    menu = tmenu.MenuState("alpha-0", [(f"Action {i}", f"v{i}") for i in range(30)], index=12)
    lines = _screen_lines([_tall_group(tmp_path, 40)], menu, height=20)
    text = "\n".join(lines)
    assert len(lines) <= 20
    assert "NAME" in text  # the table is cut from below, keeping its header
    assert re.search(r"▸ \[\w\] Action 12\b", text)
    assert "Enter" in lines[-2]  # the hint line, right above the bottom border


def test_render_without_height_draws_a_long_menu_whole(tmp_path):
    menu = tmenu.MenuState("alpha-0", [(f"Action {i}", f"v{i}") for i in range(30)], index=25)
    frame = tframe.render(
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
    g_noop = dmodel.RepoGroup(
        "alpha", "/repos/alpha", tmp_path / "a.yaml", [_ci("alpha-one", "alpha")]
    )
    out = _render_text(tframe.render([g_noop], selected=None, now=now, git_enabled=True))
    # Check the header, not the empty cells.
    header_line = next(ln for ln in out.splitlines() if "NAME" in ln)
    assert " JOB " not in header_line
    # The compact phase must also be absent when no job is in flight.
    assert "clone" not in out

    # A container with an in-flight job -> JOB column present, phase value visible.
    c = _ci("alpha-two", "alpha")
    c.job_phase = "cloning"
    g_op = dmodel.RepoGroup("alpha", "/repos/alpha", tmp_path / "a.yaml", [c])
    out2 = _render_text(tframe.render([g_op], selected=None, now=now, git_enabled=True))
    assert "clone" in out2
    header_line2 = next(ln for ln in out2.splitlines() if "NAME" in ln)
    assert " JOB " in header_line2 or header_line2.startswith("JOB ")


def test_render_shows_repo_headers_and_rows(tmp_path):
    groups = [
        dmodel.RepoGroup(
            "alpha",
            "/repos/alpha",
            tmp_path / "alpha/.jailbee/config.yaml",
            [_ci("alpha-one", "alpha")],
        ),
        dmodel.RepoGroup("gamma", None, None, [_ci("gamma-x", "gamma")]),
    ]
    now = datetime(2026, 6, 8, 12, 0, tzinfo=UTC)
    out = _render_text(
        tframe.render(
            groups,
            selected=dmodel.Row("container", "alpha-one"),
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
            tframe.render(
                groups,
                selected=dmodel.Row("container", "alpha-one"),
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
        dmodel.RepoGroup("alpha", "/repos/alpha", None, [_ci("alpha-one", "alpha")]),
        dmodel.RepoGroup("beta", "/repos/beta", None, [_ci("beta-two", "beta")]),
    ]
    out = _render_text(
        tframe.render(
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
        dmodel.RepoGroup("alpha", "/repos/alpha", None, []),
        dmodel.RepoGroup("beta", "/repos/beta", None, [_ci("beta-two", "beta")]),
    ]
    out = _render_text(
        tframe.render(
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
    group = dmodel.RepoGroup("alpha", "/repos/alpha", None, [_ci("alpha-one", "alpha")])
    out = _render_text(
        tframe.render(
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
        tframe.render(
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
    group = dmodel.RepoGroup(prefix, str(tmp_path), None, [_ci(f"{prefix}-one", prefix)])
    out = _render_text(
        tframe.render(
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
            tframe.render(
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
    group = dmodel.RepoGroup("empty", str(tmp_path), None, [])
    out = _render_text(
        tframe.render(
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
        tframe.render(
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
    group = dmodel.RepoGroup(
        "long-repository-prefix",
        str(tmp_path),
        None,
        [_ci("long-repository-prefix-one", "long-repository-prefix")],
    )
    rendered = _render_text(
        tframe.render(
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
    narrow = _header(_frame_at([group], width=40))
    wide = _header(_frame_at([group], width=200))
    shifted = _header(_frame_at([group], width=40, offset=1))
    assert "\u203a" in narrow and "PR" not in narrow.split()
    assert narrow.index("MODE") == wide.index("MODE")
    assert narrow.index("ST") == wide.index("ST")
    _, widths = dcolumns._frame_columns(
        [group],
        now=datetime(2026, 6, 8, tzinfo=UTC),
        enabled=_WIDE,
        folded=frozenset(),
        column_widths=None,
        shown_columns=None,
    )
    assert narrow.index("MODE") - narrow.index("NAME") == widths[0]
    assert shifted.index("\u2039") - shifted.index("NAME") == widths[0]
    assert shifted.index("ST") == narrow.index("MODE") + 3


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
    frame = tframe.render(
        [group],
        dmodel.Row("container", "alpha-one"),
        now=datetime(2026, 6, 8, tzinfo=UTC),
        git_enabled=True,
        enabled=_WIDE,
        column_offset=2,
    )
    cursor = _cursor_lines(_render_ansi_lines(frame, width=40))
    assert len(cursor) == 1 and "one" in dcolumns.Text.from_ansi(cursor[0]).plain
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


def test_render_with_no_enabled_columns_has_no_scroll_marks(tmp_path):
    out = _frame_at([_wide_group(tmp_path)], width=32, offset=3, enabled=())
    assert "alpha" in out
    assert "\u2039" not in out and "\u203a" not in out
    assert all(len(line) <= 32 for line in out.splitlines())


def test_render_keeps_only_enabled_column_at_tiny_width(tmp_path):
    group = dmodel.RepoGroup("alpha", str(tmp_path), None, [_ci("alpha-one", "alpha")])
    frame = tframe.render(
        [group],
        selected=None,
        now=datetime(2026, 6, 8, tzinfo=UTC),
        git_enabled=True,
        enabled=("state",),
    )

    assert "ST" in _render_text(frame, width=20)


def test_render_column_offsets_align_across_repos_of_different_lengths(tmp_path):
    groups = [
        dmodel.RepoGroup("a", "/a", None, [_ci("a-one", "a")]),
        dmodel.RepoGroup(
            "a-much-longer-repository",
            "/b",
            None,
            [_ci("a-much-longer-repository-two", "a-much-longer-repository")],
        ),
    ]
    out = _render_text(
        tframe.render(
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
    g = dmodel.RepoGroup(
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
        tframe.render(
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
        tframe.render(
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
    g = dmodel.RepoGroup("alpha", "/repos/alpha", tmp_path / "a.yaml", [_ci("alpha-one", "alpha")])
    assert "⟳" not in _title_line([g])


def test_render_title_is_left_aligned(tmp_path):
    """Left-aligned, so a widening title grows rightwards instead of shifting."""
    g = dmodel.RepoGroup("alpha", "/repos/alpha", tmp_path / "a.yaml", [_ci("alpha-one", "alpha")])
    line = _title_line([g])
    assert line.index("🐝 jailbee dashboard") <= 3


def test_render_title_carries_the_no_git_marker(tmp_path):
    """`--no-git` is constant for the run, so it belongs in the title."""
    g = dmodel.RepoGroup("alpha", "/repos/alpha", tmp_path / "a.yaml", [_ci("alpha-one", "alpha")])
    assert "no-git" in _title_line([g], git_enabled=False)
    assert "no-git" not in _title_line([g], git_enabled=True)


def test_render_subtitle_is_empty_without_a_notice(tmp_path):
    """The refresh timing moved into the title; the subtitle is notice-only."""
    g = dmodel.RepoGroup("alpha", "/repos/alpha", tmp_path / "a.yaml", [_ci("alpha-one", "alpha")])
    out = _render_text(
        tframe.render(
            [g],
            selected=None,
            now=datetime(2026, 6, 8, 12, 0, tzinfo=UTC),
            git_enabled=True,
        )
    )
    assert "refreshed" not in out


def test_render_long_notice_wraps_below_the_table_instead_of_the_border(tmp_path):
    """A long CLI message is shown whole, not cut on the bottom border."""
    g = dmodel.RepoGroup("alpha", "/repos/alpha", tmp_path / "a.yaml", [_ci("alpha-one", "alpha")])
    notice = "✗ invalid credential group name 'Bad Name': " + "lowercase letters " * 12 + "END"
    lines = (
        _render_text(
            tframe.render(
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
    g = dmodel.RepoGroup("alpha", "/repos/alpha", tmp_path / "a.yaml", [_ci("alpha-one", "alpha")])
    out = _render_text(
        tframe.render(
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
    g = dmodel.RepoGroup(
        "alpha",
        "/repos/alpha",
        tmp_path / "a.yaml",
        [_ci("alpha-one", "alpha"), _ci("alpha-two", "alpha")],
    )
    menu = tmenu.MenuState(
        "alpha-one", [("Attach tmux", "tmux"), ("Outbox", "outbox browse")], index=1
    )
    out = _render_text(
        tframe.render(
            [g],
            selected=dmodel.Row("container", "alpha-one"),
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
    g = dmodel.RepoGroup("alpha", "/repos/alpha", tmp_path / "a.yaml", [_ci("alpha-one", "alpha")])
    kwargs = {
        "selected": dmodel.Row("container", "alpha-one"),
        "now": datetime(2026, 6, 8, 12, 0, tzinfo=UTC),
        "git_enabled": True,
    }
    browsing = _render_text(tframe.render([g], **kwargs))
    menu_open = _render_text(
        tframe.render(
            [g],
            **kwargs,
            overlay=tmenu.MenuState("alpha-one", [("Attach tmux", "tmux")], index=0),
        )
    )
    assert "h/? help" in browsing.splitlines()[0]
    assert "Enter menu" not in browsing
    assert "Space fold" not in browsing
    assert "Esc" in menu_open and "cancel" in menu_open


def test_small_width_keeps_help_cue_in_the_top_border(tmp_path):
    g = dmodel.RepoGroup("alpha", str(tmp_path), None, [_ci("alpha-one", "alpha")])
    out = _render_text(
        tframe.render(
            [g],
            selected=None,
            now=datetime(2026, 6, 8, tzinfo=UTC),
            git_enabled=True,
        ),
        width=42,
    )
    assert "h/? help" in out.splitlines()[0]


def test_render_shows_a_notice_and_omits_it_when_none(tmp_path):
    g = dmodel.RepoGroup("alpha", "/repos/alpha", tmp_path / "a.yaml", [_ci("alpha-one", "alpha")])
    kwargs = {
        "selected": None,
        "now": datetime(2026, 6, 8, 12, 0, tzinfo=UTC),
        "git_enabled": True,
    }
    with_notice = _render_text(tframe.render([g], **kwargs, notice="alpha-one is view-only"))
    without = _render_text(tframe.render([g], **kwargs))

    assert "view-only" in with_notice
    assert "view-only" not in without


# --- terminal (xterm/tmux) window title -----------------------------------------


def _title_groups(tmp_path):
    return [
        dmodel.RepoGroup("alpha", "/repos/alpha", tmp_path / "a.yaml", [_ci("alpha-one", "alpha")]),
        dmodel.RepoGroup("gamma", None, None, [_ci("gamma-x", "gamma")]),
    ]


def test_terminal_title_names_the_repo_and_the_selected_container(tmp_path):
    groups = _title_groups(tmp_path)
    title = tterm.terminal_title(groups, dmodel.Row("container", "alpha-one"))
    assert title == "🐝 alpha/one"


def test_terminal_title_on_a_repo_header_names_only_the_repo(tmp_path):
    groups = _title_groups(tmp_path)
    assert tterm.terminal_title(groups, dmodel.Row("repo", "alpha")) == "🐝 alpha"


def test_terminal_title_uses_the_full_name_for_an_orphan_container(tmp_path):
    """An orphan group stripped no prefix, so neither does the title —
    matching the NAME column."""
    groups = _title_groups(tmp_path)
    title = tterm.terminal_title(groups, dmodel.Row("container", "gamma-x"))
    assert title == "🐝 gamma/gamma-x"


def test_terminal_title_without_a_selection_falls_back_to_the_tool_name(tmp_path):
    assert tterm.terminal_title(_title_groups(tmp_path), None) == "🐝 jailbee"


def test_terminal_title_of_an_unknown_container_falls_back(tmp_path):
    groups = _title_groups(tmp_path)
    assert tterm.terminal_title(groups, dmodel.Row("container", "ghost")) == "🐝 jailbee"


def test_title_sequence_is_one_osc2_sequence():
    assert tterm.title_sequence("🐝 alpha/one") == "\x1b]2;🐝 alpha/one\x07"


def test_terminal_title_scope_pushes_on_entry_and_pops_on_exit():
    """Without the pop the terminal keeps the bee title after `q`."""
    stream = io.StringIO()
    with tterm.terminal_title_scope(stream):
        assert stream.getvalue() == "\x1b[22;2t"
    assert stream.getvalue() == "\x1b[22;2t\x1b[23;2t"


def test_terminal_title_scope_pops_even_when_the_body_raises():
    stream = io.StringIO()
    with contextlib.suppress(RuntimeError), tterm.terminal_title_scope(stream):
        raise RuntimeError("boom")
    assert stream.getvalue().endswith("\x1b[23;2t")


# --- creating a container from the dashboard --------------------------------


def _create_groups(tmp_path):
    return [
        dmodel.RepoGroup("alpha", "/repos/alpha", tmp_path / "a.yaml", [_ci("alpha-one", "alpha")]),
        dmodel.RepoGroup("gamma", None, None, [_ci("gamma-x", "gamma")]),
    ]


def test_new_container_target_from_a_container_row(tmp_path):
    groups = _create_groups(tmp_path)
    target = dmenus.new_container_target(groups, dmodel.Row("container", "alpha-one"))
    assert target is groups[0]


def test_new_container_target_from_a_repo_header(tmp_path):
    groups = _create_groups(tmp_path)
    assert dmenus.new_container_target(groups, dmodel.Row("repo", "alpha")) is groups[0]


def test_new_container_target_is_none_without_a_selection(tmp_path):
    assert dmenus.new_container_target(_create_groups(tmp_path), None) is None


def test_new_container_target_is_none_for_a_stale_selection(tmp_path):
    groups = _create_groups(tmp_path)
    assert dmenus.new_container_target(groups, dmodel.Row("container", "ghost")) is None
    assert dmenus.new_container_target(groups, dmodel.Row("repo", "ghost")) is None


def test_new_container_target_is_none_for_an_orphan_group(tmp_path):
    """An orphan group has no repo config to create against — the same reason
    it gets no action menu."""
    groups = _create_groups(tmp_path)
    assert dmenus.new_container_target(groups, dmodel.Row("container", "gamma-x")) is None
    assert dmenus.new_container_target(groups, dmodel.Row("repo", "gamma")) is None


def test_new_container_target_accepts_a_scratch_group():
    """A repo with no config file has a root to create in — `jailbee new` run
    there synthesizes the same config the dashboard displayed."""
    groups = [dmodel.RepoGroup("delta", "/repos/delta", None, [_ci("delta-x", "delta")])]

    assert dmenus.new_container_target(groups, dmodel.Row("repo", "delta")) is groups[0]
    assert dmenus.new_container_target(groups, dmodel.Row("container", "delta-x")) is groups[0]


def test_new_container_reject_note_for_prefix_is_none_for_a_scratch_group():
    groups = [dmodel.RepoGroup("delta", "/repos/delta", None, [_ci("delta-x", "delta")])]

    assert dmenus.new_container_reject_note_for_prefix(groups, "delta") is None


def test_new_container_reject_note_is_none_when_creation_is_possible(tmp_path):
    groups = _create_groups(tmp_path)
    assert dmenus.new_container_reject_note(groups, dmodel.Row("repo", "alpha")) is None


def test_new_container_reject_note_asks_for_a_selection(tmp_path):
    note = dmenus.new_container_reject_note(_create_groups(tmp_path), None)
    assert note is not None and "elect" in note


def test_new_container_reject_note_names_the_orphan_repo(tmp_path):
    """'Nothing happened' is indistinguishable from broken — the note has to
    say which repo has no config."""
    groups = _create_groups(tmp_path)
    note = dmenus.new_container_reject_note(groups, dmodel.Row("repo", "gamma"))
    assert note is not None and "gamma" in note


def test_new_container_reject_note_says_a_vanished_repo_row_is_gone(tmp_path):
    """A repo row whose group disappeared between frames must not be told to
    "select a repo" — one was selected. Same wording the container branch
    uses for the same cause."""
    groups = _create_groups(tmp_path)
    note = dmenus.new_container_reject_note(groups, dmodel.Row("repo", "ghost"))
    assert note == "'ghost' is no longer listed"


def test_new_container_reject_note_phrases_a_vanished_row_the_same_way(tmp_path):
    """The two row kinds must not describe one situation differently."""
    groups = _create_groups(tmp_path)
    assert dmenus.new_container_reject_note(
        groups, dmodel.Row("repo", "ghost")
    ) == dmenus.new_container_reject_note(groups, dmodel.Row("container", "ghost"))


def test_new_container_reject_note_for_prefix_is_none_when_creation_is_possible(tmp_path):
    groups = _create_groups(tmp_path)
    assert dmenus.new_container_reject_note_for_prefix(groups, "alpha") is None


def test_new_container_reject_note_for_prefix_asks_for_a_selection_when_empty(tmp_path):
    note = dmenus.new_container_reject_note_for_prefix(_create_groups(tmp_path), "")
    assert note is not None and "elect" in note


def test_new_container_reject_note_for_prefix_asks_for_a_selection_when_unknown(tmp_path):
    note = dmenus.new_container_reject_note_for_prefix(_create_groups(tmp_path), "ghost")
    assert note is not None and "elect" in note


def test_new_container_reject_note_for_prefix_names_the_orphan_repo(tmp_path):
    groups = _create_groups(tmp_path)
    note = dmenus.new_container_reject_note_for_prefix(groups, "gamma")
    assert note is not None and "gamma" in note


def test_host_branches_lists_the_groups_repo_minus_the_excluded(mocker, tmp_path):
    lb = mocker.patch("jailbee.git.list_branches", return_value=["main", "feat/a", "dev"])
    assert dmenus.host_branches(str(tmp_path), exclude="feat/a") == ("main", "dev")
    lb.assert_called_once_with(tmp_path)


def test_host_branches_is_empty_without_a_repo_root(mocker):
    lb = mocker.patch("jailbee.git.list_branches")
    assert dmenus.host_branches(None) == ()
    lb.assert_not_called()


def test_retarget_argv_puts_the_names_after_the_separator():
    assert tsession.dact.retarget_argv("alpha-x", "main") == [
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

    assert dmenus.new_container_base_default(str(tmp_path)) == "config-improvements"
    assert get.call_args.args[0] == Path(str(tmp_path))


def test_new_container_base_default_is_none_on_a_detached_head(mocker, tmp_path):
    mocker.patch("jailbee.git.get_current_branch", return_value=None)
    assert dmenus.new_container_base_default(str(tmp_path)) is None


def test_new_container_base_default_is_none_without_a_repo_root(mocker):
    """An orphan group has no root to read; git must not be invoked at all."""
    get = mocker.patch("jailbee.git.get_current_branch")
    assert dmenus.new_container_base_default(None) is None
    get.assert_not_called()


def test_new_container_argv_passes_the_base_positionally(tmp_path):
    """`--from-base` is the golden-image alias, not a git base. The base
    branch is `jailbee new`'s second positional or it is nothing."""
    config_path = tmp_path / ".jailbee" / "config.yaml"
    target = dmodel.RepoTarget(tmp_path, config_path)
    assert dmenus.new_container_argv(target, "dashboard-fixes", "config-improvements") == [
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
    argv = dmenus.new_container_argv(dmodel.RepoTarget(tmp_path, None), "feat/x", "main")

    assert argv == ["jailbee", "new", "--background", "--", "feat/x", "main"]


@pytest.mark.parametrize("answer", ["--mount", "--yes", "--config=/tmp/evil.yaml", "-m"])
def test_new_container_argv_never_reads_a_typed_answer_as_an_option(tmp_path, answer):
    """Branch and base are typed free text. Without `--`, a branch "--mount"
    would give the container the host repo read-write."""
    for branch, base in ((answer, "main"), ("feat", answer)):
        argv = dmenus.new_container_argv(dmodel.RepoTarget(tmp_path, None), branch, base)
        separator = argv.index("--")
        assert argv[separator + 1 :] == [branch, base]


def test_new_container_argv_separator_really_stops_option_parsing(tmp_path):
    """Parse with the real `jailbee new` command, so the `--` is proven to be
    honoured by Click rather than merely present in the list."""
    from typer.main import get_command

    from jailbee.cli import app as cli_app

    argv = dmenus.new_container_argv(dmodel.RepoTarget(tmp_path, None), "--mount", "--yes")
    command = get_command(cli_app).commands["new"]  # type: ignore[attr-defined]

    ctx = command.make_context("new", argv[2:])

    assert ctx.params["background"] is True
    assert ctx.params["mount"] is False
    assert ctx.params["yes"] is False
    assert ctx.params["container_branch"] == "--mount"


def test_new_container_argv_carries_no_yes_flag(tmp_path):
    """--yes would accept a network-widening branch autostart config unseen."""
    argv = dmenus.new_container_argv(_dispatch_target(tmp_path, "c.yaml"), "b", "base")
    assert "--yes" not in argv and "-y" not in argv


def test_new_pr_container_argv_targets_configured_repo_without_yes(tmp_path):
    target = _dispatch_target(tmp_path, "c.yaml")

    assert dmenus.new_pr_container_argv(target, 123) == [
        "jailbee",
        "new",
        "--config",
        str(target.config_path),
        "--background",
        "--pr",
        "123",
    ]


def test_new_pr_container_argv_targets_scratch_repo(tmp_path):
    assert dmenus.new_pr_container_argv(dmodel.RepoTarget(tmp_path, None), 123) == [
        "jailbee",
        "new",
        "--background",
        "--pr",
        "123",
    ]


def test_parse_key_maps_arrows_and_letters():
    assert tkeys.parse_key(b"\x1b[A") == "up"
    assert tkeys.parse_key(b"\x1b[B") == "down"
    assert tkeys.parse_key(b"k") == "up"
    assert tkeys.parse_key(b"j") == "down"
    assert tkeys.parse_key(b"\r") == "enter"
    assert tkeys.parse_key(b"\n") == "enter"
    assert tkeys.parse_key(b"r") == "refresh"
    assert tkeys.parse_key(b"q") == "quit"
    assert tkeys.parse_key(b"Z") == ""  # unmapped


def test_parse_key_maps_the_quick_action_keys():
    assert tkeys.parse_key(b"t") == "action:tmux"
    assert tkeys.parse_key(b"s") == "action:shell"
    assert tkeys.parse_key(b"i") == "action:ide"
    assert tkeys.parse_key(b"c") == "action:chrome"
    assert tkeys.parse_key(b"p") == "action:pr"
    assert tkeys.parse_key(b"h") == "help"
    assert tkeys.parse_key(b"?") == "help"


def test_parse_key_maps_the_workflow_action_keys():
    assert tkeys.parse_key(b"P") == "action:pr-update"
    assert tkeys.parse_key(b"u") == "action:push"
    assert tkeys.parse_key(b"d") == "action:diff"
    assert tkeys.parse_key(b"D") == "action:destroy"


def test_quick_verb_destroy_key_follows_the_menu_gate(tmp_path):
    running = dmodel.RepoGroup(
        "alpha", str(tmp_path), tmp_path / "c.yaml", [_ci("alpha-x", "alpha")]
    )
    orphan = dmodel.RepoGroup("gone", None, None, [_ci("gone-x", "gone")])

    assert tkeys.quick_verb([running], "alpha-x", "action:destroy") == "destroy"
    assert tkeys.quick_verb([orphan], "gone-x", "action:destroy") is None


def test_quick_verb_separates_open_pr_from_update_pr(tmp_path):
    """`p` opens the PR in a browser, `P` pushes to it — the gate matches the
    verb exactly, so the two never collapse into one another."""
    with_pr = dmodel.RepoGroup(
        "alpha",
        str(tmp_path),
        tmp_path / "c.yaml",
        [_ci("alpha-x", "alpha", pr_number=7)],
    )

    assert tkeys.quick_verb([with_pr], "alpha-x", "action:pr") == "pr --open"
    assert tkeys.quick_verb([with_pr], "alpha-x", "action:pr-update") == "pr"


def test_quick_verb_workflow_keys_follow_the_menu_gate(tmp_path):
    running = dmodel.RepoGroup(
        "alpha", str(tmp_path), tmp_path / "c.yaml", [_ci("alpha-x", "alpha")]
    )
    mounted = dmodel.RepoGroup(
        "alpha", str(tmp_path), tmp_path / "c.yaml", [_ci("alpha-m", "alpha", mode="mount")]
    )
    clean = dmodel.RepoGroup(
        "alpha",
        str(tmp_path),
        tmp_path / "c.yaml",
        [_ci("alpha-c", "alpha", git_status=_dirty(wt="clean", ahead_count="0"))],
    )

    assert tkeys.quick_verb([running], "alpha-x", "action:push") == "git push"
    assert tkeys.quick_verb([running], "alpha-x", "action:diff") == "git diff"
    assert tkeys.quick_verb([mounted], "alpha-m", "action:push") is None
    assert tkeys.quick_verb([clean], "alpha-c", "action:diff") is None


def test_key_bindings_are_the_only_source_of_parse_key():
    """Every declared key sequence parses to its binding's token, and nothing
    is declared twice — the table is what `parse_key` is built from."""
    seen: dict[bytes, str] = {}
    for b in tkeys.KEY_BINDINGS:
        assert b.keys, f"{b.token} declares no keys"
        for key in b.keys:
            assert key not in seen, f"{key!r} bound twice ({seen.get(key)} and {b.token})"
            seen[key] = b.token
            assert tkeys.parse_key(key) == b.token
    tokens = [b.token for b in tkeys.KEY_BINDINGS]
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
            for _label, verb in dmenus.menu_actions(
                _ctx(
                    state=state,
                    apps=_apps("ide", "chrome"),
                    pr_number=7,
                    job_clearable=True,
                    has_job=True,
                )
            )
        }
    quick = {b.verb for b in tkeys.KEY_BINDINGS if b.verb is not None}
    assert quick, "no quick-action keys declared"
    assert quick <= offered, f"unknown verbs: {quick - offered}"


def test_quick_verb_returns_the_verb_when_the_action_is_offered(tmp_path):
    group = dmodel.RepoGroup("alpha", str(tmp_path), tmp_path / "c.yaml", [_ci("alpha-x", "alpha")])

    assert tkeys.quick_verb([group], "alpha-x", "action:tmux") == "tmux"
    assert tkeys.quick_verb([group], "alpha-x", "action:shell") == "shell"


def test_quick_verb_is_none_when_the_action_is_not_offered(tmp_path):
    """The gate is `actions_for_container`, so every rule lives in
    `menu_actions` alone — no second copy of "when is tmux allowed"."""
    running = dmodel.RepoGroup(
        "alpha", str(tmp_path), tmp_path / "c.yaml", [_ci("alpha-x", "alpha")]
    )
    stopped = dmodel.RepoGroup(
        "alpha", str(tmp_path), tmp_path / "c.yaml", [_ci("alpha-x", "alpha", state="Stopped")]
    )
    orphan = dmodel.RepoGroup("gamma", None, None, [_ci("gamma-x", "gamma")])

    assert tkeys.quick_verb([stopped], "alpha-x", "action:tmux") is None  # not running
    assert tkeys.quick_verb([running], "alpha-x", "action:ide") is None  # jetbrains off
    assert tkeys.quick_verb([running], "alpha-x", "action:chrome") is None  # chrome off
    assert tkeys.quick_verb([running], "alpha-x", "action:pr") is None  # no PR known
    assert tkeys.quick_verb([orphan], "gamma-x", "action:tmux") is None  # view-only
    assert tkeys.quick_verb([running], "alpha-nope", "action:tmux") is None  # unknown
    assert tkeys.quick_verb([running], None, "action:tmux") is None
    assert tkeys.quick_verb([running], "alpha-x", "refresh") is None  # not an action key


def test_quick_verb_follows_the_repos_apps(tmp_path):
    group = dmodel.RepoGroup(
        "alpha",
        str(tmp_path),
        tmp_path / "c.yaml",
        [_ci("alpha-x", "alpha")],
        apps=_apps("ide", "chrome"),
    )

    assert tkeys.quick_verb([group], "alpha-x", "action:ide") == "ide"
    assert tkeys.quick_verb([group], "alpha-x", "action:chrome") == "chrome"


def test_quick_reject_note_names_the_key_and_the_container(tmp_path):
    stopped = dmodel.RepoGroup(
        "alpha", str(tmp_path), tmp_path / "c.yaml", [_ci("alpha-x", "alpha", state="Stopped")]
    )

    note = tkeys.quick_reject_note([stopped], "alpha-x", "action:tmux")

    assert "'t'" in note and "tmux" in note and "alpha-x" in note


def test_quick_reject_note_reports_remote_policy_denial(tmp_path):
    from jailbee.config.models_remote import RemoteCommandPolicy, RemoteSSHConfig

    group = dmodel.RepoGroup("alpha", str(tmp_path), None, [_ci("alpha-x", "alpha")])
    policy = RemoteSSHConfig(commands=RemoteCommandPolicy(mode="allowlist", allow=["git merge"]))

    note = tkeys.quick_reject_note(
        [group], "alpha-x", "action:push", ssh_policy=policy, over_ssh=True
    )

    assert note == "Jailbee command is not allowed: git push"


def test_quick_reject_note_prefers_the_view_only_explanation():
    orphan = dmodel.RepoGroup("gamma", None, None, [_ci("gamma-x", "gamma")])

    assert tkeys.quick_reject_note([orphan], "gamma-x", "action:tmux") == dmenus.view_only_note(
        [orphan], "gamma-x"
    )


def test_quick_reject_note_handles_an_empty_selection():
    assert "selected" in tkeys.quick_reject_note([], None, "action:tmux")


def test_binding_for_token_finds_the_key_and_its_label():
    binding = tkeys.binding_for_token("action:tmux")

    assert binding is not None
    assert binding.hint == "t"
    assert binding.label
    assert tkeys.binding_for_token("nope") is None


def test_render_help_overlay_documents_every_key(tmp_path):
    g = dmodel.RepoGroup("alpha", "/repos/alpha", tmp_path / "a.yaml", [_ci("alpha-one", "alpha")])
    out = _render_text(
        tframe.render(
            [g],
            selected=dmodel.Row("container", "alpha-one"),
            now=datetime(2026, 6, 8, 12, 0, tzinfo=UTC),
            git_enabled=True,
            overlay="help",
        )
    )
    for b in tkeys.KEY_BINDINGS:
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
    g = dmodel.RepoGroup("alpha", "/repos/alpha", tmp_path / "a.yaml", [_ci("alpha-one", "alpha")])
    out = _render_text(
        tframe.render(
            [g],
            selected=dmodel.Row("container", "alpha-one"),
            now=datetime(2026, 6, 8, 12, 0, tzinfo=UTC),
            git_enabled=True,
            overlay=tmenu.MenuState("alpha-one", [("Attach tmux", "tmux")]),
        )
    )
    assert "Enter open/run" in out and "Esc cancel" in out
    assert "[key] pick" in out
    assert "h/? help" in out.splitlines()[0]


def test_render_menu_submenu_title_and_contextual_back_hint(tmp_path):
    g = dmodel.RepoGroup("alpha", "/repos/alpha", tmp_path / "a.yaml", [_ci("alpha-x", "alpha")])
    root = _grouped_menu()
    submenu, _ = tmenu.enter_menu(tmenu.move_menu(tmenu.move_menu(root, 1), 1))

    def frame(menu):
        return _render_text(
            tframe.render(
                [g],
                selected=dmodel.Row("container", "alpha-x"),
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
    assert tkeys.parse_key(b"\x1b") == "cancel"  # bare Esc (arrows are \x1b[…)
    assert tkeys.parse_key(b"\x03") == "interrupt"  # Ctrl-C
    assert tkeys.parse_key(b"") == "interrupt"  # EOF (stdin closed)


# ---------------------------------------------------------------------------
# CLI wiring test
# ---------------------------------------------------------------------------

from typer.testing import CliRunner  # noqa: E402

from jailbee.cli import app  # noqa: E402


def test_render_shows_memory_used_and_limit(tmp_path):
    c = _ci("alpha-one", "alpha")
    c.memory_usage = 4_000_000_000
    c.memory_limit = "8GiB"
    g = dmodel.RepoGroup("alpha", "/repos/alpha", tmp_path / "a.yaml", [c])
    now = datetime(2026, 6, 8, 12, 0, tzinfo=UTC)
    out = _render_text(
        tframe.render(
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

    run = mocker.patch("jailbee.dashboard.tui.app.run", return_value=0)
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
    run = mocker.patch("jailbee.dashboard.tui.app.run", return_value=0)
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

    run = mocker.patch("jailbee.dashboard.tui.app.run", return_value=0)
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
    run = mocker.patch("jailbee.dashboard.tui.app.run", return_value=0)
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
    run = mocker.patch("jailbee.dashboard.tui.app.run", return_value=0)
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
    run = mocker.patch("jailbee.dashboard.tui.app.run")
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
    run = mocker.patch("jailbee.dashboard.tui.app.run", return_value=0)
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

    target = dmodel.RepoTarget(tmp_path, None)
    run = mocker.patch("jailbee.dashboard.tui.session.subprocess.run")
    policy = RemoteSSHConfig(commands=RemoteCommandPolicy(mode="disabled"))

    with pytest.raises(RouteError, match="disabled"):
        ddispatch._dispatch_action(
            target,
            "merge",
            "alpha",
            remote=True,
            over_ssh=True,
            ssh_policy=policy,
        )

    run.assert_not_called()


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
    mocker.patch("jailbee.dashboard.tui.app.run", return_value=0)
    mocker.patch("jailbee.incus.Incus")
    mocker.patch("jailbee.config.load_repo_config", side_effect=RuntimeError("bug"))
    result = CliRunner().invoke(app, ["dashboard"])
    assert result.exit_code != 0
    assert isinstance(result.exception, RuntimeError)


def test_tui_command_is_an_alias_for_the_dashboard(mocker):
    """`jailbee tui` mirrors `jailbee gui`: the TUI frontend, same options."""
    from jailbee.config import ConfigNotFoundError

    run = mocker.patch("jailbee.dashboard.tui.app.run", return_value=0)
    mocker.patch("jailbee.incus.Incus")
    mocker.patch("jailbee.config.load_repo_config", side_effect=ConfigNotFoundError("none"))
    result = CliRunner().invoke(app, ["tui", "-i", "5", "--git-interval", "7", "--no-git"])
    assert result.exit_code == 0
    _, kwargs = run.call_args
    assert {"interval", "git_interval", "no_git"}.isdisjoint(kwargs)


def test_menu_actions_clear_job_entry_follows_session_when_clearable():
    actions = dmenus.menu_actions(_ctx(job_clearable=True))
    assert actions[:4] == [
        ("Attach tmux", "tmux"),
        ("Open shell", "shell"),
        ("Outbox", "outbox browse"),
        ("Clear failed job", "job clear"),
    ]


def test_menu_actions_no_clear_entry_when_not_clearable():
    actions = dmenus.menu_actions(_ctx())
    assert "job clear" not in [verb for _, verb in actions]


def test_menu_actions_clear_job_precedes_open_pr():
    actions = dmenus.menu_actions(_ctx(pr_number=7, job_clearable=True))
    verbs = [verb for _, verb in actions]
    assert verbs.index("job clear") < verbs.index("pr --open")


def test_menu_actions_clear_job_offered_for_a_container_with_no_state():
    # A job row whose container never existed is rendered with state "—".
    actions = dmenus.menu_actions(_ctx(state="—", job_clearable=True))
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
    groups = [dmodel.RepoGroup("p", "/repo", Path("/repo/.jailbee/config.yaml"), [c])]

    verbs = [verb for _, verb in dmenus.actions_for_container(groups, "p-foo")]

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
    groups = [dmodel.RepoGroup("p", "/repo", Path("/repo/.jailbee/config.yaml"), [c])]

    verbs = [verb for _, verb in dmenus.actions_for_container(groups, "p-foo")]

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
    groups = [dmodel.RepoGroup("p", "/repo", Path("/repo/.jailbee/config.yaml"), [c])]

    verbs = [verb for _, verb in dmenus.actions_for_container(groups, "p-foo")]

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
    groups = [dmodel.RepoGroup("p", "/repo", Path("/repo/.jailbee/config.yaml"), [c])]

    verbs = [verb for _, verb in dmenus.actions_for_container(groups, "p-foo")]

    assert "job clear" not in verbs


def test_fold_target_works_from_a_header_and_from_a_container():
    """Resolve the group for either a header or one of its container rows."""
    groups = [dmodel.RepoGroup("a", "/a", None, [_ci("a-1", "a")])]
    assert dmodel.fold_target(groups, dmodel.Row("repo", "a")) == "a"
    assert dmodel.fold_target(groups, dmodel.Row("container", "a-1")) == "a"
    assert dmodel.fold_target(groups, dmodel.Row("container", "gone")) is None
    assert dmodel.fold_target(groups, None) is None


def test_toggle_folded_is_a_pure_set_flip():
    assert dmodel.toggle_folded(frozenset(), "a") == frozenset({"a"})
    assert dmodel.toggle_folded(frozenset({"a", "b"}), "a") == frozenset({"b"})


def test_render_marks_a_folded_group_and_hides_its_rows(tmp_path):
    now = datetime(2026, 6, 8, 12, 0, tzinfo=UTC)
    groups = [
        dmodel.RepoGroup("alpha", "/a", tmp_path / "a.yaml", [_ci("alpha-one", "alpha")]),
        dmodel.RepoGroup("beta", "/b", tmp_path / "b.yaml", [_ci("beta-two", "beta")]),
    ]
    out = _render_text(
        tframe.render(
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
    g = dmodel.RepoGroup("alpha", "/a", tmp_path / "a.yaml", [_ci("alpha-one", "alpha")])
    kwargs = dict(
        now=datetime(2026, 6, 8, 12, 0, tzinfo=UTC),
        git_enabled=True,
    )

    unselected = _render_text(tframe.render([g], selected=None, **kwargs))
    on_header = _render_text(tframe.render([g], selected=dmodel.Row("repo", "alpha"), **kwargs))
    # The plain text is identical: nothing is inserted, so nothing shifts.
    assert unselected == on_header

    plain = tframe.repo_heading(g, None, frozenset())
    selected = tframe.repo_heading(g, dmodel.Row("repo", "alpha"), frozenset())
    assert selected.plain == plain.plain
    assert str(plain.style) == "bold cyan"
    # Exactly the style a selected container row gets from `repo_table`.
    row = tframe.repo_table(g, [], (), dmodel.Row("container", "alpha-one"))
    assert str(selected.style) == str(row.rows[0].style) == tframe.CURSOR_STYLE


def test_cursor_style_is_distinct_from_every_heading_colour(tmp_path):
    """The cursor must never look like a heading's resting colour, or the
    cursor on that heading would be invisible."""
    repo = dmodel.RepoGroup("alpha", "/a", None, [])
    orphan = dmodel.RepoGroup("gamma", None, None, [])
    resting = {str(tframe.repo_heading(g, None, frozenset()).style) for g in (repo, orphan)}
    assert resting == {"bold cyan", "bold yellow"}
    assert tframe.CURSOR_STYLE == "bold magenta"
    for g in (repo, orphan):
        on_it = tframe.repo_heading(g, dmodel.Row("repo", g.prefix), frozenset())
        assert str(on_it.style) == tframe.CURSOR_STYLE


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
    g = dmodel.RepoGroup("alpha", "/a", tmp_path / "a.yaml", [_ci("alpha-one", "alpha")])
    out = _render_text(
        tframe.render(
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
        dmodel.RepoGroup(
            "alpha",
            "/a",
            tmp_path / "a.yaml",
            [_ci("alpha-one", "alpha"), _ci("alpha-two", "alpha")],
        )
    ]
    out = _render_text(
        tframe.render(
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
        dmodel.RepoGroup("alpha", "/a", tmp_path / "a.yaml", [with_pr]),
        dmodel.RepoGroup("beta", "/b", tmp_path / "b.yaml", [_ci("beta-one", "beta")]),
    ]
    kwargs = dict(
        selected=None,
        now=now,
        git_enabled=True,
        shown_columns=dcolumns.nonempty_columns(groups, now=now),
    )
    unfolded = _render_text(tframe.render(groups, **kwargs))
    folded = _render_text(tframe.render(groups, folded=frozenset({"alpha"}), **kwargs))

    assert "PR" in unfolded
    assert "PR" in folded


def test_space_key_is_fold_key_and_enter_remains_bound():
    assert tkeys.parse_key(b" ") == "space"
    binding = tkeys.binding_for_token("space")
    assert binding is not None
    assert binding.hint and binding.label
    assert tkeys.parse_key(b"\r") == "enter"


def test_space_is_the_fold_key_and_still_toggles_settings_binding():
    assert tkeys.parse_key(b" ") == "space"
    binding = tkeys.binding_for_token("space")
    assert binding is not None
    assert "fold" in binding.label and "Settings" in binding.label


def test_settings_key_is_bound_to_f2_and_shift_s():
    """Both F2 encodings, because terminals disagree, plus a letter that works
    everywhere. `s` is already shell, so the alias is `S`."""
    assert tkeys.parse_key(b"\x1bOQ") == "settings"
    assert tkeys.parse_key(b"\x1b[12~") == "settings"
    assert tkeys.parse_key(b"S") == "settings"
    binding = tkeys.binding_for_token("settings")
    assert binding is not None and binding.hint


def test_all_column_names_is_the_full_ls_vocabulary():
    """The Fields tab offers every real column, including ones off by default
    in both views — that is the point of an enabled set over a hide list."""
    from datetime import UTC, datetime

    from jailbee.lifecycle import ls_field_specs

    names = dcolumns.all_column_names()
    expected = [f.name for f in ls_field_specs(now=datetime(2026, 6, 8, tzinfo=UTC))]
    assert list(names) == expected
    assert "full_name" in names and "git_status" in names and "ip" in names


def test_dynamic_column_names_are_exactly_the_show_if_ones():
    assert dcolumns.dynamic_column_names() == frozenset(
        {"job", "ttl", "pr", "issues", "mode", "group"}
    )


def test_settings_repo_prefixes_keeps_a_folded_repo_that_is_not_on_screen():
    """Otherwise a repo whose containers are gone could never be unfolded:
    it draws no group, so the Repos tab would not list it."""
    groups = [
        dmodel.RepoGroup("alpha", "/a", None, [_ci("alpha-one", "alpha")]),
        dmodel.RepoGroup("empty", "/e", None, []),
    ]
    prefixes = dcolumns.settings_repo_prefixes(groups, frozenset({"alpha", "vanished"}))

    assert "alpha" in prefixes
    assert "vanished" in prefixes  # folded but absent — still reachable
    assert "empty" in prefixes
    assert len(prefixes) == len(set(prefixes))  # no duplicate for a folded on-screen repo


def test_render_draws_the_settings_overlay_below_the_table(tmp_path):
    from jailbee.dashboard.settings import open_settings

    now = datetime(2026, 6, 8, 12, 0, tzinfo=UTC)
    g = dmodel.RepoGroup("alpha", "/a", tmp_path / "a.yaml", [_ci("alpha-one", "alpha")])
    overlay = open_settings(
        field_names=dcolumns.all_column_names(),
        enabled=frozenset(dcolumns.default_columns()),
        repo_prefixes=("alpha",),
        folded=frozenset(),
    )
    out = _render_text(
        tframe.render(
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


_RIGHT, _LEFT = b"\x1b[C", b"\x1b[D"


def test_arrow_keys_parse_and_are_documented():
    assert tkeys.parse_key(_RIGHT) == "scroll-right"
    assert tkeys.parse_key(_LEFT) == "scroll-left"
    out = _render_text(
        tframe.render(
            [], None, now=datetime(2026, 6, 8, tzinfo=UTC), git_enabled=False, overlay="help"
        )
    )
    assert "←/→" in out and "scroll columns" in out


def test_clamp_column_offset_follows_the_width(tmp_path):
    kw = dict(
        now=datetime(2026, 6, 8, tzinfo=UTC),
        enabled=_WIDE,
        folded=frozenset(),
        column_widths=None,
        shown_columns=None,
    )
    group = _wide_group(tmp_path)
    assert dcolumns.clamp_column_offset([group], 9, available=40, **kw) > 0
    assert dcolumns.clamp_column_offset([group], 9, available=296, **kw) == 0
    assert dcolumns.clamp_column_offset([group], -1, available=40, **kw) == 0
    assert (
        dcolumns.clamp_column_offset(
            [group], 9, available=40, **(kw | {"folded": frozenset({"alpha"})})
        )
        == 0
    )


# --- Repo-level CLI entries (apply, diagnostics, prune) ----------------------


def test_repo_menu_offers_apply_after_network_and_before_fold(tmp_path):
    menu = tmenu.open_repo_menu([_cfg_group(tmp_path)], "alpha", frozenset())
    assert menu is not None
    labels = [i.label if isinstance(i, dmenus.MenuGroup) else i[0] for i in menu.actions]
    assert labels.index("Network →") < labels.index("Apply config…") < labels.index("Fold")


def test_orphan_repo_menu_offers_no_apply():
    group = dmodel.RepoGroup("orphan", None, None, [_ci("orphan-x", "orphan")])
    assert "apply" not in _repo_menu_verbs(tmenu.open_repo_menu([group], "orphan", frozenset()))


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
    menu = tmenu.open_repo_menu(
        [_cfg_group(tmp_path)],
        "alpha",
        frozenset(),
        ssh_policy=_ssh_policy(policy_kwargs),
        over_ssh=over_ssh,
    )
    assert ("apply" in _repo_menu_verbs(menu)) is offered


# --- Credential group… in the repo menu ------------------------------------


def test_repo_menu_offers_credential_group_after_the_creation_entries():
    group = dmodel.RepoGroup("alpha", "/alpha", None, [])
    menu = tmenu.open_repo_menu([group], "alpha", frozenset())
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
    group = dmodel.RepoGroup("alpha", "/alpha", None, [])
    menu = tmenu.open_repo_menu(
        [group], "alpha", frozenset(), ssh_policy=_ssh_policy(policy_kwargs), over_ssh=over_ssh
    )
    assert menu is not None
    verbs = [item[1] for item in menu.actions if not isinstance(item, dmenus.MenuGroup)]
    assert ("credential-group" in verbs) is offered


# --- Credential group… in the container menu --------------------------------


def test_container_menu_offers_credential_group_just_before_network_and_lifecycle(tmp_path):
    # The offered leaves keep it before network; the terminal draws it after
    # the Git/PR/Lifecycle/Network block.
    group = dmodel.RepoGroup("alpha", str(tmp_path), None, [_ci("alpha-x", "alpha")])
    menu = tmenu.open_menu([group], "alpha-x")
    assert menu is not None
    verbs = [verb for _label, verb in menu.actions]
    at = verbs.index("credential-group")
    assert verbs[at + 1].startswith("net ")
    assert not any(v.startswith("net ") for v in verbs[:at])
    assert at < verbs.index("restart")
    # in the drawn menu it sits right above the Network → group
    entries = list(tmenu._menu_entries(menu))
    labels = [i.label if isinstance(i, dmenus.MenuGroup) else i[0] for i in entries]
    assert labels.index("Network →") < entries.index(_CREDENTIAL_GROUP_LEAF)


def test_stopped_container_menu_offers_credential_group_before_egress(tmp_path):
    group = dmodel.RepoGroup("alpha", str(tmp_path), None, [_ci("alpha-x", "alpha", "Stopped")])
    menu = tmenu.open_menu([group], "alpha-x")
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
    placed = [verb for _label, verb in dmenus._with_credential_group(actions)]
    assert placed[: len(expected)] == expected
    assert placed.count("credential-group") == 1


def test_shared_action_list_stays_free_of_the_terminal_only_entry(tmp_path):
    """The Qt dashboard reads `actions_for_container`; it has no handler for the verb."""
    group = dmodel.RepoGroup("alpha", str(tmp_path), None, [_ci("alpha-x", "alpha")])
    assert "credential-group" not in {
        v for _l, v in dmenus.actions_for_container([group], "alpha-x")
    }


@_CREDENTIAL_GROUP_POLICY_CASES
def test_container_menu_credential_group_follows_the_ssh_policy(
    tmp_path, over_ssh, policy_kwargs, offered
):
    group = dmodel.RepoGroup("alpha", str(tmp_path), None, [_ci("alpha-x", "alpha")])
    menu = tmenu.open_menu(
        [group],
        "alpha-x",
        remote=over_ssh,
        over_ssh=over_ssh,
        ssh_policy=_ssh_policy(policy_kwargs),
    )
    assert menu is not None
    assert (_CREDENTIAL_GROUP_LEAF in menu.actions) is offered


def test_parse_key_maps_n_to_the_new_container_token():
    assert tkeys.parse_key(b"n") == "new"


def test_new_binding_is_not_a_container_verb():
    """`n` is repo-scoped. A `verb` would put it through `quick_verb`, which
    gates on a *container's* state and would reject it everywhere."""
    binding = tkeys.binding_for_token("new")
    assert binding is not None
    assert binding.verb is None


def test_new_binding_appears_in_the_help_overlay(tmp_path):
    g = dmodel.RepoGroup("alpha", "/repos/alpha", tmp_path / "a.yaml", [_ci("alpha-one", "alpha")])
    out = _render_text(
        tframe.render(
            [g],
            selected=None,
            now=datetime(2026, 6, 8, 12, 0, tzinfo=UTC),
            git_enabled=True,
            overlay="help",
        )
    )
    assert "create a container" in out


def test_e_and_shift_e_are_bound_and_documented():
    from jailbee.dashboard.tui.keys import KEY_BINDINGS, parse_key

    assert parse_key(b"e") == "config-edit"
    assert parse_key(b"E") == "config-edit-global"
    tokens = {b.token for b in KEY_BINDINGS}
    assert {"config-edit", "config-edit-global"} <= tokens
    # The pair documents itself once, the way up/down does.
    hints = [b.hint for b in KEY_BINDINGS if b.token.startswith("config-edit")]
    assert hints.count("") == 1


def test_config_edit_reject_note_names_configuring_not_creating():
    from jailbee.dashboard.menus import config_edit_reject_note_for_prefix

    assert config_edit_reject_note_for_prefix([], "") == "Select a repo or a container first"
    group = dmodel.RepoGroup(prefix="orphan", repo_root=None, config_path=None, containers=[])
    note = config_edit_reject_note_for_prefix([group], "orphan")
    assert note is not None
    assert "config" in note
    assert "create" not in note


def test_config_edit_reject_note_accepts_a_real_repo(tmp_path):
    from jailbee.dashboard.menus import config_edit_reject_note_for_prefix

    group = dmodel.RepoGroup(
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
    from jailbee.dashboard.menus import config_edit_reject_note_for_prefix

    group = dmodel.RepoGroup(
        prefix="demo", repo_root=str(tmp_path), config_path=None, containers=[]
    )

    note = config_edit_reject_note_for_prefix([group], "demo")

    assert note is not None
    assert "scratch.config" in note
    assert "config init" in note
    assert config_edit_reject_note_for_prefix([group], "demo", global_layer=True) is None


def test_sample_activity_flattens_every_group(mocker):
    """One reading covers the whole screen — not one per repo group."""
    annotate = mocker.patch.object(dmodel, "annotate_activity")
    # `_ci(name, repo, ...)` — two positional arguments, see its definition
    # near the top of this test module.
    a = _ci("p-a", "p")
    b = _ci("q-b", "q")
    groups = [
        dmodel.RepoGroup("p", "/p", Path("/p/.jailbee/config.yaml"), [a]),
        dmodel.RepoGroup("q", "/q", Path("/q/.jailbee/config.yaml"), [b]),
    ]
    sampler = mocker.Mock()

    dmodel.sample_activity(groups, sampler)

    annotate.assert_called_once_with([a, b], sampler)


def test_sample_activity_matches_agents_per_group_never_across_repos(mocker):
    """Each group reads its own containers' session homes, from one sampler reading."""
    mocker.patch.object(dmodel, "annotate_activity")
    agents = mocker.patch.object(dmodel, "annotate_agent_status")
    p_home = (("p-a", "claude", Path("/s/p/.private/p-a/claude")),)
    q_home = (("q-b", "claude", Path("/s/q/.private/q-b/claude")),)
    by_homes = {p_home: {"p-a": ["p-session"]}, q_home: {"q-b": ["q-session"]}}
    mocker.patch("jailbee.agent_status.read_sessions", side_effect=lambda h: by_homes[tuple(h)])
    a, b = _ci("p-a", "p"), _ci("q-b", "q")
    groups = [
        dmodel.RepoGroup("p", "/p", None, [a], agent_homes=p_home),
        dmodel.RepoGroup("q", "/q", None, [b], agent_homes=q_home),
    ]
    sampler = mocker.Mock()

    dmodel.sample_activity(groups, sampler)

    assert [c.args for c in agents.call_args_list] == [
        ([a], {"p-a": ["p-session"]}, sampler),
        ([b], {"q-b": ["q-session"]}, sampler),
    ]


def test_sample_activity_gives_each_group_a_lookup_over_its_own_config_homes(mocker):
    from jailbee.agent_activity import ActivityReader

    mocker.patch.object(dmodel, "annotate_activity")
    agents = mocker.patch.object(dmodel, "annotate_agent_status")
    mocker.patch("jailbee.agent_status.read_sessions", return_value={})
    reader = mocker.Mock(spec=ActivityReader)
    reader.lookup_for.side_effect = lambda homes, processes: ("lookup", dict(homes))
    p_homes = (("p-a", "claude", Path("/s/p/claude")),)
    groups = [
        dmodel.RepoGroup("p", "/p", None, [_ci("p-a", "p")], agent_config_homes=p_homes),
        dmodel.RepoGroup("q", "/q", None, [_ci("q-b", "q")]),
    ]
    sampler = mocker.Mock()

    dmodel.sample_activity(groups, sampler, reader)

    assert [c.kwargs["activity"] for c in agents.call_args_list] == [
        ("lookup", {("p-a", "claude"): Path("/s/p/claude")}),
        ("lookup", {}),
    ]
    reader.begin.assert_called_once_with()
    reader.finish.assert_called_once_with()
    assert reader.lookup_for.call_args_list[0].args[1] == sampler.processes


def test_sample_activity_without_a_reader_asks_for_no_activity(mocker):
    mocker.patch.object(dmodel, "annotate_activity")
    agents = mocker.patch.object(dmodel, "annotate_agent_status")
    mocker.patch("jailbee.agent_status.read_sessions", return_value={})
    groups = [dmodel.RepoGroup("p", "/p", None, [_ci("p-a", "p")])]

    dmodel.sample_activity(groups, mocker.Mock())

    assert agents.call_args.kwargs == {"activity": None}


def test_a_failing_group_still_lets_the_reader_finish(mocker):
    from jailbee.agent_activity import ActivityReader

    mocker.patch.object(dmodel, "annotate_activity")
    mocker.patch.object(dmodel, "annotate_agent_status", side_effect=RuntimeError("boom"))
    mocker.patch("jailbee.agent_status.read_sessions", return_value={})
    reader = mocker.Mock(spec=ActivityReader)
    groups = [dmodel.RepoGroup("p", "/p", None, [_ci("p-a", "p")])]

    dmodel.sample_activity(groups, mocker.Mock(), reader)

    reader.finish.assert_called_once_with()


def test_a_failing_lookup_still_lets_the_reader_finish(mocker):
    """`lookup_for` runs outside the per-group guard, so only `finally` closes the tick."""
    from jailbee.agent_activity import ActivityReader

    mocker.patch.object(dmodel, "annotate_activity")
    mocker.patch.object(dmodel, "annotate_agent_status")
    reader = mocker.Mock(spec=ActivityReader)
    reader.lookup_for.side_effect = RuntimeError("boom")
    groups = [dmodel.RepoGroup("p", "/p", None, [_ci("p-a", "p")])]

    with pytest.raises(RuntimeError):
        dmodel.sample_activity(groups, mocker.Mock(), reader)

    reader.finish.assert_called_once_with()


def test_sample_activity_survives_a_group_whose_agent_reading_fails(mocker):
    """One group's failure clears that group's AGENT and leaves the others."""
    mocker.patch.object(dmodel, "annotate_activity")
    agents = mocker.patch.object(
        dmodel, "annotate_agent_status", side_effect=[RuntimeError("boom"), None]
    )
    mocker.patch("jailbee.agent_status.read_sessions", return_value={})
    a, b = _ci("p-a", "p"), _ci("q-b", "q")
    a.agent_status = (mocker.Mock(),)
    groups = [
        dmodel.RepoGroup("p", "/p", None, [a]),
        dmodel.RepoGroup("q", "/q", None, [b]),
    ]

    dmodel.sample_activity(groups, mocker.Mock())

    assert agents.call_count == 2
    assert a.agent_status == ()


def test_sample_activity_reads_the_agent_state_after_the_activity_reading(mocker):
    """AGENT uses the reading `annotate_activity` just took."""
    order: list[str] = []
    mocker.patch.object(
        dmodel, "annotate_activity", side_effect=lambda *a: order.append("activity")
    )
    mocker.patch.object(
        dmodel, "annotate_agent_status", side_effect=lambda *a, **k: order.append("agent")
    )
    groups = [dmodel.RepoGroup("p", "/p", None, [_ci("p-a", "p")])]

    dmodel.sample_activity(groups, mocker.Mock())

    assert order == ["activity", "agent"]


def test_remote_action_menu_never_opens_the_pr_in_a_host_browser():
    local = dmenus.menu_actions(_ctx(pr_number=7))
    remote = dmenus.menu_actions(_ctx(pr_number=7, remote=True))

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
    run = mocker.patch("jailbee.dashboard.tui.app.run", return_value=0)
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
    assert dmenus.group_menu_actions(leaves, include_network=True, terminal_order=True) == [
        leaves[4],
        leaves[7],
        leaves[0],
        dmenus.MenuGroup("Git →", (leaves[5], leaves[6])),
        dmenus.MenuGroup("PR →", (leaves[2], leaves[3])),
        dmenus.MenuGroup("Network →", (leaves[8],)),
    ]


def test_terminal_order_running_menu_layout(tmp_path):
    apps = [dmodel.AppMenuEntry("chrome", "Chrome")]
    group = dmodel.RepoGroup(
        "alpha", str(tmp_path), None, [_ci("alpha-x", "alpha", pr_number=7)], apps=apps
    )
    menu = tmenu.open_menu([group], "alpha-x")
    assert menu is not None
    labels = [
        item.label if isinstance(item, dmenus.MenuGroup) else item[0]
        for item in tmenu._menu_entries(menu)
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
        for i in tmenu._menu_entries(menu)
        if isinstance(i, dmenus.MenuGroup) and i.label == "Lifecycle →"
    )
    assert [v for _, v in lifecycle.actions] == ["restart", "stop", "destroy"]
    # Leaves stay offered: the `s` and `D` keys gate on them.
    assert {"shell", "destroy"} <= {v for _, v in menu.actions}


def test_terminal_order_stopped_menu_keeps_a_lone_destroy_leaf(tmp_path):
    group = dmodel.RepoGroup(
        "alpha", str(tmp_path), None, [_ci("alpha-x", "alpha", "Stopped", pr_number=7)]
    )
    menu = tmenu.open_menu([group], "alpha-x")
    assert menu is not None
    labels = [
        item.label if isinstance(item, dmenus.MenuGroup) else item[0]
        for item in tmenu._menu_entries(menu)
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
    assert dmenus.group_menu_actions(leaves) == [
        dmenus.MenuGroup("PR →", (leaves[0], leaves[1])),
        dmenus.MenuGroup("Git →", (leaves[2],)),
    ]


def test_terminal_menu_drops_an_empty_pr_group_when_only_apply_remains():
    # Mount mode: no Create/update PR, only the pending apply — the apply is
    # hoisted and the PR → group must not survive as an empty shell.
    actions = dmenus.menu_actions(_ctx(mode="mount", git_status=_dirty(pending_pr_actions=2)))
    menu = tmenu.MenuState("alpha-x", actions)
    labels = [
        item.label if isinstance(item, dmenus.MenuGroup) else item[0]
        for item in tmenu._menu_entries(menu)
    ]
    assert labels[0] == "Outbox (2 pending)"
    assert "PR →" not in labels


# --- The Accounts panel (A) --------------------------------------------------


def test_repo_menu_offers_accounts_after_the_credential_group():
    group = dmodel.RepoGroup("alpha", "/alpha", None, [])
    menu = tmenu.open_repo_menu([group], "alpha", frozenset())
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
    group = dmodel.RepoGroup("alpha", "/alpha", None, [])
    menu = tmenu.open_repo_menu(
        [group], "alpha", frozenset(), ssh_policy=_ssh_policy(policy_kwargs), over_ssh=over_ssh
    )
    assert menu is not None
    verbs = [item[1] for item in menu.actions if not isinstance(item, dmenus.MenuGroup)]
    assert ("accounts" in verbs) is offered


def test_accounts_key_is_documented_in_help():
    assert tkeys.parse_key(b"A") == "accounts"
    out = _render_text(tframe._render_help())
    line = next(ln for ln in out.splitlines() if "credential groups and stored logins" in ln)
    assert line.split()[1] == "A"
    assert "Accounts panel: Enter acts on a login or group, n creates a group." in out


# --- _run_cli_foreground: dashboard-built argv in the real terminal -----------


def _target(tmp_path: Path) -> dmodel.RepoTarget:
    return dmodel.RepoTarget(tmp_path, tmp_path / "c.yaml")


def test_run_cli_foreground_inserts_config_before_the_separator_and_pauses(mocker, tmp_path):
    run = mocker.patch.object(tsession.subprocess, "run")
    run.return_value.returncode = 0
    wait = patch_pause(mocker)

    rc = ddispatch._run_cli_foreground(
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

    run = mocker.patch.object(tsession.subprocess, "run")
    run.return_value.returncode = 0
    patch_pause(mocker)

    ddispatch._run_cli_foreground(
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

    run = mocker.patch.object(tsession.subprocess, "run")
    paged = mocker.patch.object(ddispatch, "_run_paged")

    with pytest.raises(RouteError):
        ddispatch._run_cli_foreground(
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
    mocker.patch.object(ddispatch, "pager_argv", return_value=["less", "-R"])
    paged = mocker.patch.object(ddispatch, "_run_paged", return_value=3)
    run = mocker.patch.object(tsession.subprocess, "run")

    rc = ddispatch._run_cli_foreground(_target(tmp_path), ["doctor"], style="paged")

    assert rc == 3
    paged.assert_called_once_with(
        ["jailbee", "doctor", "--config", str(tmp_path / "c.yaml")], ["less", "-R"], tmp_path
    )
    run.assert_not_called()


def test_run_cli_foreground_never_pages_a_remote_session(mocker, tmp_path):
    from jailbee.config.models_remote import RemoteSSHConfig

    mocker.patch.object(ddispatch, "pager_argv", return_value=["less", "-R"])
    paged = mocker.patch.object(ddispatch, "_run_paged")
    run = mocker.patch.object(tsession.subprocess, "run")
    run.return_value.returncode = 0
    wait = patch_pause(mocker)

    ddispatch._run_cli_foreground(
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
    mocker.patch.object(ddispatch, "pager_argv", return_value=["less", "-R"])
    mocker.patch.object(
        ddispatch, "_run_paged", side_effect=ddispatch._PagerUnavailableError("gone")
    )
    run = mocker.patch.object(tsession.subprocess, "run")
    run.return_value.returncode = 0
    wait = patch_pause(mocker)

    assert ddispatch._run_cli_foreground(_target(tmp_path), ["doctor"], style="paged") == 0
    run.assert_called_once()
    wait.assert_called_once()


def test_run_cli_foreground_plain_does_not_pause(mocker, tmp_path):
    run = mocker.patch.object(tsession.subprocess, "run")
    run.return_value.returncode = 0
    wait = patch_pause(mocker)

    ddispatch._run_cli_foreground(_target(tmp_path), ["disk-usage"], style="plain")

    wait.assert_not_called()


@pytest.mark.parametrize(("remote", "over_ssh"), [(False, True), (True, False)])
def test_run_cli_foreground_either_remote_flag_alone_forbids_the_pager(
    mocker, tmp_path, remote, over_ssh
):
    from jailbee.config.models_remote import RemoteSSHConfig

    pager = mocker.patch.object(ddispatch, "pager_argv", return_value=["less", "-R"])
    paged = mocker.patch.object(ddispatch, "_run_paged")
    run = mocker.patch.object(tsession.subprocess, "run")
    run.return_value.returncode = 0
    wait = patch_pause(mocker)

    ddispatch._run_cli_foreground(
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
    menu = tmenu.open_repo_menu([_cfg_group(tmp_path)], "alpha", frozenset())
    assert menu is not None
    labels = [i.label if isinstance(i, dmenus.MenuGroup) else i[0] for i in menu.actions]
    at = labels.index("Diagnostics →")
    assert labels[at - 1] == "Apply config…"
    assert labels[at + 1 : at + 3] == ["Prune stale containers…", "Fold"]
    diagnostics = menu.actions[at]
    assert isinstance(diagnostics, dmenus.MenuGroup)
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
    menu = tmenu.open_repo_menu(
        [_cfg_group(tmp_path)],
        "alpha",
        frozenset(),
        ssh_policy=_ssh_policy(policy_kwargs),
        over_ssh=over_ssh,
    )
    assert _repo_menu_verbs(menu) & {"doctor", "disk-usage", "prune"} == expected
    assert menu is not None
    labels = [i.label for i in menu.actions if isinstance(i, dmenus.MenuGroup)]
    # the submenu is dropped, not left empty, when both leaves are refused
    assert ("Diagnostics →" in labels) is bool(expected & {"doctor", "disk-usage"})


# --- Terminal-only container entries (autostart, snapshots, mounts) ---------


def test_container_menu_places_autostart_after_the_job_log_and_snapshots_before_the_group(
    tmp_path,
):
    menu = tmenu.open_menu([_cfg_group(tmp_path, (_autostart_ci(),))], "alpha-x")
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
    shared = {verb for _label, verb in dmenus.actions_for_container([group], "alpha-x")}
    assert not shared & dmenus.TERMINAL_MENU_VERBS


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
    placed = [verb for _label, verb in dmenus._insert_after_job(actions, [("X", "X")])]
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
    menu = tmenu.open_menu(
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


# --- Mount… / Unmount… (menu entries) ----------------------------------------


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
    menu = tmenu.open_menu(
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
    assert dmenus.actions_for_container([group], "alpha-x", **kwargs) == []

    menu = tmenu.open_menu([group], "alpha-x", **kwargs)

    if expected is None:
        assert menu is None
    else:
        assert menu is not None
        assert [label for label, _verb in menu.actions] == expected


def test_help_panel_points_at_the_repo_and_container_menu_entries():
    text = _render_text(tframe._render_help())
    assert "Apply config…" in text
    assert "Snapshots…" in text


_CONTAINER_VERB_CASES = sorted(tsession.dact.CONTAINER_VERBS)


def test_every_container_verb_has_a_guard_case(tmp_path):
    group = _every_verb_group(tmp_path)
    menu = tmenu.open_menu([group], "alpha-x")
    assert menu is not None
    offered = {verb for _label, verb in menu.actions}
    # a new verb must be offered by this fixture (and so parametrized below)
    assert offered & tsession.dact.CONTAINER_VERBS == tsession.dact.CONTAINER_VERBS
    assert set(_CONTAINER_VERB_CASES) == tsession.dact.CONTAINER_VERBS


def test_outbox_dispatch_rechecks_ssh_policy(mocker, tmp_path):
    from jailbee.config.models_remote import RemoteCommandPolicy, RemoteSSHConfig
    from jailbee.remote_ssh.router import RouteError

    child = mocker.patch.object(tsession.subprocess, "run")
    policy = RemoteSSHConfig(commands=RemoteCommandPolicy(mode="allowlist", allow=["git merge"]))
    with pytest.raises(RouteError):
        ddispatch._dispatch_action(
            _dispatch_target(tmp_path), "outbox browse", "alpha-x", over_ssh=True, ssh_policy=policy
        )
    child.assert_not_called()


def test_gui_remote_action_menu_offers_app_launches():
    """`remote.ssh.gui` lets a remote session launch apps onto the shared display."""
    apps = [dmodel.AppMenuEntry("chrome", "Chrome")]
    ctx = _ctx(apps=apps, remote=True)
    assert not [a for a in dmenus.menu_actions(ctx) if a[1] == "chrome"]

    enabled = dmenus.menu_actions(dataclasses.replace(ctx, gui_remote=True))

    assert ("Launch Chrome", "chrome") in enabled


def test_gui_remote_quick_key_reason_follows_the_gui_switch():
    from jailbee.config.models_remote import RemoteCommandPolicy, RemoteSSHConfig

    group = dmodel.RepoGroup("alpha", "/repos/alpha", None, [_ci("alpha-x", "alpha")])
    group.apps = _apps("chrome")
    off = RemoteSSHConfig(commands=RemoteCommandPolicy(mode="full"))
    on = RemoteSSHConfig(commands=RemoteCommandPolicy(mode="full"), gui=True)
    kwargs = {"remote": True, "over_ssh": True}

    assert tkeys.quick_verb([group], "alpha-x", "action:chrome", ssh_policy=off, **kwargs) is None
    note = tkeys.quick_reject_note([group], "alpha-x", "action:chrome", ssh_policy=off, **kwargs)
    assert note == "GUI apps are not available over remote SSH"
    assert (
        tkeys.quick_verb([group], "alpha-x", "action:chrome", ssh_policy=on, **kwargs) == "chrome"
    )
    note = tkeys.quick_reject_note([group], "alpha-x", "action:chrome", ssh_policy=on, **kwargs)
    assert "GUI apps are not available" not in note


def test_gui_remote_allowlist_without_chrome_does_not_offer_chrome():
    """The feature switch is not enough: the command policy still decides."""
    from jailbee.config.models_remote import RemoteCommandPolicy, RemoteSSHConfig

    group = dmodel.RepoGroup("alpha", "/repos/alpha", None, [_ci("alpha-x", "alpha")])
    group.apps = _apps("chrome")
    policy = RemoteSSHConfig(gui=True, commands=RemoteCommandPolicy(mode="allowlist", allow=["ls"]))

    actions = dmenus.actions_for_container(
        [group], "alpha-x", remote=True, ssh_policy=policy, over_ssh=True
    )

    assert "chrome" not in {verb for _label, verb in actions}


def test_gui_remote_allowlist_naming_chrome_offers_chrome():
    from jailbee.config.models_remote import RemoteCommandPolicy, RemoteSSHConfig

    group = dmodel.RepoGroup("alpha", "/repos/alpha", None, [_ci("alpha-x", "alpha")])
    group.apps = _apps("chrome")
    policy = RemoteSSHConfig(
        gui=True, commands=RemoteCommandPolicy(mode="allowlist", allow=["chrome"])
    )

    actions = dmenus.actions_for_container(
        [group], "alpha-x", remote=True, ssh_policy=policy, over_ssh=True
    )

    assert "chrome" in {verb for _label, verb in actions}


@pytest.mark.parametrize(
    ("verb", "pauses"),
    [("chrome", True), ("apps run figma", True), ("shell", False)],
)
def test_remote_gui_dispatch_keeps_the_recipe_on_screen(mocker, tmp_path, verb, pauses):
    from jailbee.config.models_remote import RemoteCommandPolicy, RemoteSSHConfig

    run = mocker.patch.object(tsession.subprocess, "run")
    run.return_value.returncode = 0
    wait = patch_pause(mocker)
    policy = RemoteSSHConfig(commands=RemoteCommandPolicy(mode="full"), gui=True)

    ddispatch._dispatch_action(
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
    assert dmenus._is_gui_verb("chrome")
    assert dmenus._is_gui_verb("apps run figma")
    assert not dmenus._is_gui_verb("shell")


def _gui_dispatch_policy(*, gui: bool):
    from jailbee.config.models_remote import RemoteCommandPolicy, RemoteSSHConfig

    return RemoteSSHConfig(commands=RemoteCommandPolicy(mode="full"), gui=gui, restrict_host=False)


def test_dispatch_action_pauses_after_a_gui_launch_over_ssh_when_gui_is_on(mocker, tmp_path):
    """The launch prints how to reach the shared display; the pause keeps it readable."""
    run = mocker.patch.object(tsession.subprocess, "run")
    run.return_value.returncode = 0
    wait = patch_pause(mocker)

    ddispatch._dispatch_action(
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
    run = mocker.patch.object(tsession.subprocess, "run")
    run.return_value.returncode = 0
    wait = patch_pause(mocker)

    ddispatch._dispatch_action(
        _dispatch_target(tmp_path),
        "chrome",
        "alpha-x",
        over_ssh=True,
        ssh_policy=_gui_dispatch_policy(gui=True),
    )

    wait.assert_not_called()


def test_dispatch_action_does_not_pause_after_a_gui_verb_when_gui_is_off(mocker, tmp_path):
    run = mocker.patch.object(tsession.subprocess, "run")
    run.return_value.returncode = 0
    wait = patch_pause(mocker)

    ddispatch._dispatch_action(
        _dispatch_target(tmp_path),
        "chrome",
        "alpha-x",
        over_ssh=True,
        ssh_policy=_gui_dispatch_policy(gui=False),
    )

    wait.assert_not_called()


def test_window_rows_keeps_everything_that_fits():
    assert dcolumns.window_rows([1, 1, 1], 2, 3) == dcolumns.TableWindow(0, 3, 0, 0)


def test_window_rows_is_top_anchored_while_the_cursor_fits_there():
    # Four rows plus a "↓ 6 more" marker fill the five-line budget.
    assert dcolumns.window_rows([1] * 10, 1, 5) == dcolumns.TableWindow(0, 4, 0, 6)


def test_window_rows_is_bottom_anchored_near_the_end():
    assert dcolumns.window_rows([1] * 10, 9, 5) == dcolumns.TableWindow(6, 10, 6, 0)


def test_window_rows_centres_the_cursor_between_two_markers():
    w = dcolumns.window_rows([1] * 20, 10, 7)
    assert w.start <= 10 < w.stop
    assert w.stop - w.start == 5  # seven lines minus two markers
    assert (w.hidden_above, w.hidden_below) == (w.start, 20 - w.stop)


def test_window_rows_counts_wrapped_rows_by_their_height():
    heights = [1, 2, 2, 2, 2, 2, 2]
    w = dcolumns.window_rows(heights, 6, 6)
    assert w.start <= 6 < w.stop
    assert sum(heights[w.start : w.stop]) + (w.hidden_above > 0) + (w.hidden_below > 0) <= 6


def test_window_rows_without_a_cursor_starts_at_the_top():
    assert dcolumns.window_rows([1] * 10, None, 5).start == 0


def test_render_scrolls_a_long_table_to_the_cursor(tmp_path):
    frame = tframe.render(
        [_named_rows_group(tmp_path, 40)],
        dmodel.Row("container", "alpha-row35"),
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
    frame = tframe.render(
        [_named_rows_group(tmp_path, 40)],
        dmodel.Row("container", "alpha-row35"),
        now=datetime(2026, 6, 8, 12, 0, tzinfo=UTC),
        git_enabled=True,
    )
    text = _render_text(frame, width=100)
    assert "row00" in text and "row39" in text and "more" not in text


_FRAME_NOW = datetime(2026, 6, 8, 12, 0, tzinfo=UTC)


def _frame(groups, selected, *, overlay=None, height=None, width=100, show_details=True):
    frame = tframe.render(
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
    lines = _frame([_named_rows_group(tmp_path, 3)], dmodel.Row("container", "alpha-row01"))
    header = next(i for i, ln in enumerate(lines) if "NAME" in ln)
    title = next(i for i, ln in enumerate(lines) if "╭─ row01" in ln)
    assert title > header + 3  # under the heading and three container rows
    text = "\n".join(lines[title:])
    assert "network" in text and "git" in text and "state" in text


def test_details_toggled_off_draws_no_panel(tmp_path):
    lines = _frame(
        [_named_rows_group(tmp_path, 3)],
        dmodel.Row("container", "alpha-row01"),
        show_details=False,
    )
    assert "╭─ row01" not in "\n".join(lines)


def test_menu_sits_to_the_right_of_the_details(tmp_path):
    menu = tmenu.MenuState("alpha-row01", [("Shell", "shell"), ("Tmux", "tmux")])
    lines = _frame(
        [_named_rows_group(tmp_path, 3)],
        dmodel.Row("container", "alpha-row01"),
        overlay=menu,
        height=30,
    )
    shared = next(ln for ln in lines if "alpha-row01 →" in ln)
    details_at = shared.find("╭─ row01")  # the details title comes first
    menu_at = shared.find("alpha-row01 →")
    assert details_at < menu_at


def test_other_overlays_hide_the_details(tmp_path):
    picker = tsession.Picker("x", "Pick one", (tsession.PickerEntry("Entry 0", "0"),))
    lines = _frame(
        [_named_rows_group(tmp_path, 3)],
        dmodel.Row("container", "alpha-row01"),
        overlay=picker,
        height=30,
    )
    text = "\n".join(lines)
    assert "Pick one" in text
    assert "╭─ row01" not in text


def _with_activity(group: dmodel.RepoGroup, *, activity: bool) -> dmodel.RepoGroup:
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
    selected = dmodel.Row("container", "alpha-row01")
    for activity, expected in (
        (True, dd.DETAILS_MAX_ROWS + dd.DETAILS_ACTIVITY_ROWS),
        (False, dd.DETAILS_MAX_ROWS),
    ):
        group = _with_activity(_named_rows_group(tmp_path, 40), activity=activity)
        lines = _frame([group], selected, height=40)
        assert _details_content_rows(lines) == expected, activity


def test_repo_heading_shows_the_repo_summary(tmp_path):
    text = "\n".join(_frame([_named_rows_group(tmp_path, 3)], dmodel.Row("repo", "alpha")))
    assert "/repos/alpha" in text and "3 running / 3" in text


def test_long_table_keeps_min_rows_and_the_cursor_with_details(tmp_path):
    lines = _frame(
        [_named_rows_group(tmp_path, 40)],
        dmodel.Row("container", "alpha-row39"),
        height=24,
    )
    assert len(lines) <= 24
    text = "\n".join(lines)
    assert "row39" in text and "↑" in text
    header = next(i for i, ln in enumerate(lines) if "NAME" in ln)
    panel_top = next(i for i, ln in enumerate(lines) if "╭─ row39" in ln)
    assert panel_top - header - 1 >= tframe.MIN_TABLE_ROWS
    assert lines[-1].startswith("╰")


def test_short_terminals_never_overflow(tmp_path):
    menu = tmenu.MenuState("alpha-row05", [(f"Action {i}", f"v{i}") for i in range(30)], index=20)
    for height in (8, 10, 12, 16):
        lines = _frame(
            [_named_rows_group(tmp_path, 40)],
            dmodel.Row("container", "alpha-row05"),
            overlay=menu,
            height=height,
        )
        assert len(lines) <= height, height
        assert lines[-1].startswith("╰"), height


def test_v_parses_to_the_details_toggle():
    assert tkeys.parse_key(b"v") == "details"


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
    return dmodel.RepoGroup("alpha", "/repos/alpha", tmp_path / "a.yaml", containers)


def _rows_shown(lines):
    """How many container rows the table draws (the `rowNN` lines above the panel)."""
    panel = next((i for i, ln in enumerate(lines) if i and "╭" in ln), len(lines))
    return sum(1 for ln in lines[:panel] if re.search(r"row\d\d", ln))


def test_table_window_does_not_depend_on_the_highlighted_row(tmp_path):
    group = _mixed_group(tmp_path)
    shown = set()
    for selected in (
        dmodel.Row("repo", "alpha"),
        dmodel.Row("container", "alpha-row10"),
        dmodel.Row("container", "alpha-row11"),
        dmodel.Row("container", "alpha-row12"),
    ):
        lines = _frame([group], selected, height=30, width=80)
        assert len(lines) <= 30 and lines[-1].startswith("╰")
        shown.add((_rows_shown(lines), next(i for i, ln in enumerate(lines) if i and "╭" in ln)))
    assert len(shown) == 1, shown


@pytest.mark.parametrize("height", [8, 10, 12, 14])
def test_cursor_row_is_visible_and_the_frame_fits_at_small_heights(tmp_path, height):
    for row in ("alpha-row00", "alpha-row21", "alpha-row39"):
        lines = _frame(
            [_mixed_group(tmp_path)], dmodel.Row("container", row), height=height, width=80
        )
        assert len(lines) <= height, (height, row)
        assert lines[-1].startswith("╰"), (height, row)
        assert row.removeprefix("alpha-") in "\n".join(lines), (height, row)


@pytest.mark.parametrize("height", [8, 10, 12])
def test_a_panel_too_small_for_two_rows_is_dropped(tmp_path, height):
    lines = _frame([_mixed_group(tmp_path)], dmodel.Row("container", "alpha-row21"), height=height)
    assert "…" not in "\n".join(lines)
    assert not any(i and "╭" in ln for i, ln in enumerate(lines))


def test_a_menu_alone_replaces_a_panel_that_does_not_fit(tmp_path):
    menu = tmenu.MenuState("alpha-row21", [("Shell", "shell"), ("Tmux", "tmux")])
    lines = _frame(
        [_mixed_group(tmp_path)],
        dmodel.Row("container", "alpha-row21"),
        overlay=menu,
        height=10,
    )
    text = "\n".join(lines)
    assert "alpha-row21 →" in text and "network" not in text
    assert "row21" in text and lines[-1].startswith("╰") and len(lines) <= 10


def test_the_table_keeps_min_rows_with_the_panel_at_height_14(tmp_path):
    """Pins MIN_TABLE_ROWS: at 14 the panel gets what is left after the floor."""
    lines = _frame(
        [_mixed_group(tmp_path)], dmodel.Row("container", "alpha-row21"), height=14, width=80
    )
    # Literal on purpose: MIN_TABLE_ROWS (5) lines under the header, two of
    # them taken by the "more" markers.
    assert _rows_shown(lines) >= 3


def test_a_narrow_terminal_drops_the_details_beside_a_menu(tmp_path):
    menu = tmenu.MenuState("alpha-row21", [("Shell", "shell"), ("Tmux", "tmux")])
    lines = _frame(
        [_mixed_group(tmp_path)],
        dmodel.Row("container", "alpha-row21"),
        overlay=menu,
        height=30,
        width=40,
    )
    text = "\n".join(lines)
    assert "alpha-row21 →" in text and "network" not in text


def test_gather_live_adds_extra_roots_to_the_registered_ones(mocker, tmp_path):
    registered = tmp_path / "reg"
    extra = tmp_path / "extra"
    mocker.patch.object(dmodel, "registered_repo_roots", return_value=[registered, extra])
    rows = mocker.patch.object(dmodel, "gather_rows", return_value=[])
    incus = mocker.Mock()

    dmodel.gather_live(incus, [extra], with_git=True)

    rows.assert_called_once_with(incus, [extra, registered], with_git=True)


def _group(prefix, root=None):
    return dmodel.RepoGroup(prefix, root, None, [])


def test_present_pins_the_cwd_group_first(tmp_path):
    groups = [_group("alpha", "/a"), _group("beta", "/b"), _group("zeta")]
    shown = dmodel.present(groups, Path("/b"))
    assert [g.prefix for g in shown] == ["beta", "alpha", "zeta"]
    assert [g.prefix for g in groups] == ["alpha", "beta", "zeta"]  # input untouched


def test_present_without_cwd_keeps_the_order():
    groups = [_group("alpha", "/a"), _group("beta", "/b")]
    assert dmodel.present(groups, None) == groups


def test_present_drops_groups_the_scope_excludes():
    from jailbee.remote_ssh.repo_scope import RemoteRepoScope

    groups = [_group("alpha", "/a"), _group("secret", "/s"), _group("gamma")]
    shown = dmodel.present(groups, None, RemoteRepoScope(frozenset({"secret"})))
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
        return tframe.render(
            [dmodel.RepoGroup("alpha", str(tmp_path), None, [c])],
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
        tframe.render(
            [dmodel.RepoGroup("alpha", str(tmp_path), None, [c])],
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
            (dmodel.Row("repo", "alpha"), "╭─ alpha"),
            (dmodel.Row("container", "alpha-row01"), "╭─ row01"),
        ):
            lines = _frame([group], selected, height=height)
            start = next(i for i, line in enumerate(lines) if title in line)
            end = next(i for i in range(start + 1, len(lines)) if "╰" in lines[i])
            panels.append(lines[start : end + 1])
        assert len(panels[0]) == len(panels[1]) == dd.DETAILS_MAX_ROWS + 2


def test_optimize_key_is_documented_and_parsed():
    assert tkeys.parse_key(b"o") == "optimize"
    out = _render_text(
        tframe.render(
            [], None, now=datetime(2026, 6, 8, tzinfo=UTC), git_enabled=False, overlay="help"
        )
    )
    assert "optimize" in out.lower() and "width" in out.lower()


def test_optimized_widths_retain_snapshot_until_reoptimized(tmp_path):
    now = datetime(2026, 6, 8, tzinfo=UTC)
    short = dmodel.RepoGroup("alpha", str(tmp_path), None, [_ci("alpha-one", "alpha")])
    long = dataclasses.replace(short, containers=[_ci("alpha-abcdefghijklmnop", "alpha")])
    enabled = ("name", "network")
    widths = dcolumns.optimize_column_widths([short], now=now, enabled=enabled)

    def frame(group, budgets, width=200):
        return _render_text(
            tframe.render(
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
    updated = dcolumns.optimize_column_widths([long], now=now, enabled=enabled)
    renewed = next(
        line for line in frame(long, updated).splitlines() if "●" in line and "alpha" not in line
    )
    assert "abcdefghijklmnop" in renewed
    assert renewed.index("●") > after.index("●")


def test_nonempty_columns_hide_placeholders_without_changing_preferences(tmp_path):
    now = datetime(2026, 6, 8, tzinfo=UTC)
    enabled = ("name", "pr", "job", "network", "created")
    group = dmodel.RepoGroup("alpha", str(tmp_path), None, [_ci("alpha-one", "alpha")])
    out = _render_text(tframe.render([group], None, now=now, git_enabled=False, enabled=enabled))
    header = next(line for line in out.splitlines() if "NAME" in line)
    assert "PR" not in header and "JOB" not in header and "AGE" not in header
    assert "●" in out
    assert enabled == ("name", "pr", "job", "network", "created")


def test_optimized_widths_are_named_without_first_column_indent(tmp_path):
    now = datetime(2026, 6, 8, tzinfo=UTC)
    group = dmodel.RepoGroup("alpha", str(tmp_path), None, [_ci("alpha-one", "alpha")])
    widths = dcolumns.optimize_column_widths([group], now=now, enabled=("name", "network"))
    fields = dcolumns.visible_fields(now, group.containers, ("name", "network"))
    forward = dcolumns._dashboard_column_widths(fields, widths)
    reverse = dcolumns._dashboard_column_widths(list(reversed(fields)), widths)
    assert forward[0] == widths[fields[0].name] + 2
    assert forward[1] == widths[fields[1].name]
    assert reverse == (forward[1] + 2, forward[0] - 2)


def test_dashboard_formatting_contracts():
    from rich.text import Text

    from jailbee.dashboard.format import dashboard_header
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
        f for f in dcolumns.visible_fields(now, [container()], ["state"]) if f.name == "state"
    )
    assert state.cell(container()) == "▶"
    assert state.cell(container(state="Stopped")) == "■"
    assert state.cell(container(state="Frozen")) == "Ⅱ"
    unknown = state.cell(container(state="<custom>[Running]</custom>"))
    assert Text.from_markup(unknown).plain == "<custom>[Running]</custom>"
    assert state.json(container()) == "Running"

    network = next(
        f for f in dcolumns.visible_fields(now, [container()], ["network"]) if f.name == "network"
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
        f for f in dcolumns.visible_fields(now, [container()], ["created"]) if f.name == "created"
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
    fields = dcolumns.visible_fields(now, [container()], names)
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

    mem = next(f for f in dcolumns.visible_fields(now, [container()], ["mem"]) if f.name == "mem")
    memory = container(memory_usage=1_073_741_824)
    assert mem.cell(memory) == "1.0G/4 GiB"
    assert mem.json(memory) == {"usage": 1_073_741_824, "limit": "4 GiB"}

    content = card_content(
        container(), dcolumns.visible_fields(now, [container()], ["name", "state"])
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
        for f in dcolumns.visible_fields(
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
