"""Tests for the gie dashboard module (pure logic; no real Incus/TTY)."""

from __future__ import annotations

import contextlib
import io
import itertools
from datetime import UTC, datetime, timedelta
from pathlib import Path

from rich.console import Console, RenderableType

from jailbee import dashboard
from jailbee.git_status import GitStatus
from jailbee.lifecycle import ContainerInfo


def test_collect_config_paths_puts_cwd_first_and_dedupes(mocker):
    a = Path("/repos/a/.jailbee/config.yaml")
    b = Path("/repos/b/.jailbee/config.yaml")
    mocker.patch.object(dashboard, "registered_repo_configs", return_value=[a, b])
    # cwd config equals an already-registered one -> no duplicate, cwd wins order
    result = dashboard.collect_config_paths(b)
    assert result == [b, a]


def test_collect_config_paths_no_cwd(mocker):
    a = Path("/repos/a/.jailbee/config.yaml")
    mocker.patch.object(dashboard, "registered_repo_configs", return_value=[a])
    assert dashboard.collect_config_paths(None) == [a]


def test_collect_config_paths_empty(mocker):
    mocker.patch.object(dashboard, "registered_repo_configs", return_value=[])
    assert dashboard.collect_config_paths(None) == []


def test_registered_repo_configs_skips_missing_and_keeps_present(db_session, tmp_path, mocker):
    from jailbee.db.models import RegisteredRepo

    # One repo with a real config.yaml on disk, one stale (no file).
    present = tmp_path / "present"
    (present / ".jailbee").mkdir(parents=True)
    (present / ".jailbee" / "config.yaml").write_text("source_repo:\n  path: .\n")

    db_session.add(
        RegisteredRepo(
            container_prefix="present",
            repo_root=str(present),
            registered_at=datetime.now(UTC),
        )
    )
    db_session.add(
        RegisteredRepo(
            container_prefix="stale",
            repo_root=str(tmp_path / "nonexistent"),
            registered_at=datetime.now(UTC),
        )
    )
    db_session.commit()

    mocker.patch(
        "jailbee.db.get_engine",
        return_value=db_session.get_bind(),
    )
    result = dashboard.registered_repo_configs()
    assert result == [present / ".jailbee" / "config.yaml"]


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


def _ctx(**kw: object) -> dashboard.MenuContext:
    """A running clone container in a repo whose config loaded.

    The defaults are the common case; each test overrides only the field its
    subject is about.
    """
    fields: dict[str, object] = {
        "state": "Running",
        "has_config": True,
        "current_network": "strict",
    }
    fields.update(kw)
    return dashboard.MenuContext(**fields)


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


def test_gather_rows_groups_per_repo_and_pins_cwd_first(tmp_path, mocker, make_cfg):
    cwd_cfg = make_cfg(tmp_path / "alpha")  # container_prefix == "alpha"
    other_cfg = make_cfg(tmp_path / "beta")  # container_prefix == "beta"
    cwd_path = tmp_path / "alpha" / ".jailbee" / "config.yaml"
    other_path = tmp_path / "beta" / ".jailbee" / "config.yaml"

    def fake_load(p):
        return cwd_cfg if p == cwd_path else other_cfg

    def fake_list(cfg, incus, *, all_repos, with_git_status, with_background):
        if all_repos:
            return []  # no orphans
        if cfg is cwd_cfg:
            return [_ci("alpha-one", "alpha")]
        return [_ci("beta-one", "beta")]

    mocker.patch.object(dashboard, "load_config", side_effect=fake_load)
    mocker.patch.object(dashboard, "list_containers", side_effect=fake_list)

    groups = dashboard.gather_rows(
        mocker.MagicMock(), [other_path, cwd_path], cwd_config=cwd_path, with_git=False
    )
    # cwd group ("alpha") pinned first despite being passed second
    assert [g.prefix for g in groups] == ["alpha", "beta"]
    assert groups[0].config_path == cwd_path
    assert [c.name for c in groups[0].containers] == ["alpha-one"]


def test_gather_rows_carries_the_repos_loose_ttl_default(tmp_path, mocker, make_cfg):
    """The Qt duration dialog pre-selects this, so it must be the repo's own
    configured `loose_auto_revert.after`, not the first preset."""
    cfg = make_cfg(tmp_path / "alpha", loose_auto_revert={"after": "45m"})
    path = tmp_path / "alpha" / ".jailbee" / "config.yaml"
    mocker.patch.object(dashboard, "load_config", return_value=cfg)

    def fake_list(c, incus, *, all_repos, with_git_status, with_background):
        return [] if all_repos else [_ci("alpha-one", "alpha")]

    mocker.patch.object(dashboard, "list_containers", side_effect=fake_list)

    groups = dashboard.gather_rows(mocker.MagicMock(), [path], cwd_config=path, with_git=False)

    assert groups[0].loose_ttl_default == "45m"


def test_gather_rows_loose_ttl_default_is_none_when_policy_disabled(tmp_path, mocker, make_cfg):
    """None tells the GUI not to ask: a disabled policy schedules no TTL."""
    cfg = make_cfg(tmp_path / "alpha", loose_auto_revert={"enabled": False})
    path = tmp_path / "alpha" / ".jailbee" / "config.yaml"
    mocker.patch.object(dashboard, "load_config", return_value=cfg)

    def fake_list(c, incus, *, all_repos, with_git_status, with_background):
        return [] if all_repos else [_ci("alpha-one", "alpha")]

    mocker.patch.object(dashboard, "list_containers", side_effect=fake_list)

    groups = dashboard.gather_rows(mocker.MagicMock(), [path], cwd_config=path, with_git=False)

    assert groups[0].loose_ttl_default is None


def test_gather_rows_records_the_repos_push_defaults(tmp_path, mocker, make_cfg):
    """The Qt dashboard asks the merge/rebase question itself, and only when
    the repo left it unanswered — so the group has to carry the answer."""
    cfg = make_cfg(
        tmp_path / "alpha",
        push={"default_action": "rebase", "default_source": "current"},
    )
    path = tmp_path / "alpha" / ".jailbee" / "config.yaml"
    mocker.patch.object(dashboard, "load_config", return_value=cfg)

    def fake_list(c, incus, *, all_repos, with_git_status, with_background):
        return [] if all_repos else [_ci("alpha-one", "alpha")]

    mocker.patch.object(dashboard, "list_containers", side_effect=fake_list)

    groups = dashboard.gather_rows(mocker.MagicMock(), [path], cwd_config=path, with_git=False)

    assert groups[0].push_action_default == "rebase"
    assert groups[0].push_source_default == "current"


def test_gather_rows_push_defaults_fall_back_to_the_config_defaults(tmp_path, mocker, make_cfg):
    """PushConfig's own defaults: 'ask' is why the GUI has a dialog at all."""
    cfg = make_cfg(tmp_path / "alpha")
    path = tmp_path / "alpha" / ".jailbee" / "config.yaml"
    mocker.patch.object(dashboard, "load_config", return_value=cfg)

    def fake_list(c, incus, *, all_repos, with_git_status, with_background):
        return [] if all_repos else [_ci("alpha-one", "alpha")]

    mocker.patch.object(dashboard, "list_containers", side_effect=fake_list)

    groups = dashboard.gather_rows(mocker.MagicMock(), [path], cwd_config=path, with_git=False)

    assert groups[0].push_action_default == "ask"
    assert groups[0].push_source_default == "base"


def test_gather_rows_renders_an_int_after_as_minutes(tmp_path, mocker, make_cfg):
    cfg = make_cfg(tmp_path / "alpha", loose_auto_revert={"after": 20})
    path = tmp_path / "alpha" / ".jailbee" / "config.yaml"
    mocker.patch.object(dashboard, "load_config", return_value=cfg)

    def fake_list(c, incus, *, all_repos, with_git_status, with_background):
        return [] if all_repos else [_ci("alpha-one", "alpha")]

    mocker.patch.object(dashboard, "list_containers", side_effect=fake_list)

    groups = dashboard.gather_rows(mocker.MagicMock(), [path], cwd_config=path, with_git=False)

    assert groups[0].loose_ttl_default == "20m"


def test_gather_rows_orphan_group_has_no_loose_ttl_default(tmp_path, mocker, make_cfg):
    cfg = make_cfg(tmp_path / "alpha")
    path = tmp_path / "alpha" / ".jailbee" / "config.yaml"
    mocker.patch.object(dashboard, "load_config", return_value=cfg)

    def fake_list(c, incus, *, all_repos, with_git_status, with_background):
        if all_repos:
            return [_ci("alpha-one", "alpha"), _ci("gamma-x", "gamma")]
        return [_ci("alpha-one", "alpha")]

    mocker.patch.object(dashboard, "list_containers", side_effect=fake_list)

    groups = dashboard.gather_rows(mocker.MagicMock(), [path], cwd_config=path, with_git=False)

    orphan = next(g for g in groups if g.prefix == "gamma")
    assert orphan.loose_ttl_default is None


def test_gather_rows_surfaces_orphans_view_only(tmp_path, mocker, make_cfg):
    cfg = make_cfg(tmp_path / "alpha")
    path = tmp_path / "alpha" / ".jailbee" / "config.yaml"
    mocker.patch.object(dashboard, "load_config", return_value=cfg)

    def fake_list(c, incus, *, all_repos, with_git_status, with_background):
        if all_repos:
            return [_ci("alpha-one", "alpha"), _ci("gamma-x", "gamma")]
        return [_ci("alpha-one", "alpha")]

    mocker.patch.object(dashboard, "list_containers", side_effect=fake_list)
    groups = dashboard.gather_rows(mocker.MagicMock(), [path], cwd_config=path, with_git=False)
    orphan = next(g for g in groups if g.prefix == "gamma")
    assert orphan.config_path is None
    assert orphan.repo_root is None
    assert [c.name for c in orphan.containers] == ["gamma-x"]
    # no container appears twice
    names = [c.name for g in groups for c in g.containers]
    assert sorted(names) == ["alpha-one", "gamma-x"]


def test_gather_rows_cwd_none_orphans_sort_last(tmp_path, mocker, make_cfg):
    beta = make_cfg(tmp_path / "beta")
    alpha = make_cfg(tmp_path / "alpha")
    beta_path = tmp_path / "beta" / ".jailbee" / "config.yaml"
    alpha_path = tmp_path / "alpha" / ".jailbee" / "config.yaml"

    def fake_load(p):
        return alpha if p == alpha_path else beta

    def fake_list(cfg, incus, *, all_repos, with_git_status, with_background):
        if all_repos:
            # one orphan ('zeta') plus the two covered repos
            return [_ci("alpha-1", "alpha"), _ci("beta-1", "beta"), _ci("zeta-x", "zeta")]
        return [_ci(f"{cfg.container_prefix}-1", cfg.container_prefix)]

    mocker.patch.object(dashboard, "load_config", side_effect=fake_load)
    mocker.patch.object(dashboard, "list_containers", side_effect=fake_list)

    groups = dashboard.gather_rows(
        mocker.MagicMock(), [beta_path, alpha_path], cwd_config=None, with_git=False
    )
    # named repos alpha-sorted first, orphan group ('zeta') last
    assert [g.prefix for g in groups] == ["alpha", "beta", "zeta"]
    assert groups[-1].config_path is None  # orphan group trails


def test_gather_rows_hides_repo_with_no_containers(tmp_path, mocker, make_cfg):
    empty_cfg = make_cfg(tmp_path / "alpha")  # container_prefix == "alpha"
    populated_cfg = make_cfg(tmp_path / "beta")  # container_prefix == "beta"
    empty_path = tmp_path / "alpha" / ".jailbee" / "config.yaml"
    populated_path = tmp_path / "beta" / ".jailbee" / "config.yaml"

    def fake_load(p):
        return empty_cfg if p == empty_path else populated_cfg

    def fake_list(cfg, incus, *, all_repos, with_git_status, with_background):
        if all_repos:
            return []  # no orphans
        if cfg is empty_cfg:
            return []
        return [_ci("beta-one", "beta")]

    mocker.patch.object(dashboard, "load_config", side_effect=fake_load)
    mocker.patch.object(dashboard, "list_containers", side_effect=fake_list)

    groups = dashboard.gather_rows(
        mocker.MagicMock(), [empty_path, populated_path], cwd_config=None, with_git=False
    )
    # The empty repo produces no group at all; the populated one still appears.
    assert [g.prefix for g in groups] == ["beta"]


def test_gather_rows_empty_config_paths_returns_empty(mocker):
    # No configs -> no base_cfg -> no orphan scan -> empty result, no calls.
    lc = mocker.patch.object(dashboard, "list_containers")
    result = dashboard.gather_rows(mocker.MagicMock(), [], cwd_config=None, with_git=False)
    assert result == []
    lc.assert_not_called()


def test_gather_rows_skips_unloadable_config_never_raises(tmp_path, mocker, make_cfg):
    good = make_cfg(tmp_path / "alpha")
    good_path = tmp_path / "alpha" / ".jailbee" / "config.yaml"
    bad_path = tmp_path / "broken" / ".jailbee" / "config.yaml"

    def fake_load(p):
        if p == bad_path:
            raise OSError("gone")
        return good

    def fake_list(c, incus, *, all_repos, with_git_status, with_background):
        return [] if all_repos else [_ci("alpha-one", "alpha")]

    mocker.patch.object(dashboard, "load_config", side_effect=fake_load)
    mocker.patch.object(dashboard, "list_containers", side_effect=fake_list)
    groups = dashboard.gather_rows(
        mocker.MagicMock(), [good_path, bad_path], cwd_config=good_path, with_git=False
    )
    assert [g.prefix for g in groups] == ["alpha"]


def test_view_only_note_explains_a_config_less_group():
    groups = [dashboard.RepoGroup("gamma", None, None, [_ci("gamma-x", "gamma")])]
    note = dashboard.view_only_note(groups, "gamma-x")
    assert note is not None
    assert "gamma" in note and "view-only" in note


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


def test_gather_live_reresolves_config_paths_on_every_gather(mocker):
    """A repo registered while a dashboard is running must be picked up by the
    next gather.

    `jailbee new` in a not-yet-registered repo registers it mid-session
    (cli.py), and the 60s pool timer can unregister/re-register a repo whose
    config file momentarily vanishes (egress_pool.refresh_all). Until the
    dashboard loads that repo's config, `gather_rows` files its containers
    under a view-only orphan group, so `actions_for_container` returns [] and
    the right-click menu silently never opens.
    """
    a = Path("/repos/a/.jailbee/config.yaml")
    b = Path("/repos/b/.jailbee/config.yaml")
    registered = [a]
    mocker.patch.object(dashboard, "registered_repo_configs", side_effect=lambda: list(registered))
    gr = mocker.patch.object(dashboard, "gather_rows", return_value=[])
    incus = mocker.MagicMock()

    dashboard.gather_live(incus, None, with_git=False)
    assert gr.call_args.args[1] == [a]

    registered.append(b)  # a `jailbee new` in repo b just registered it
    dashboard.gather_live(incus, None, with_git=True)
    assert gr.call_args.args[1] == [a, b]
    assert gr.call_args.kwargs == {"cwd_config": None, "with_git": True}


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
    """Repo headers are selectable rows. That is what lets `Space` reach a
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


def test_selectable_rows_omits_an_empty_group():
    """An empty group draws no header either — `render` already skips it, and
    a cursor stop on an invisible row would be a dead keypress."""
    groups = [dashboard.RepoGroup("a", "/a", None, [])]
    assert dashboard.selectable_rows(groups) == []


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
    """The verbs from "Attach tmux" onwards — the session/lifecycle block.

    The tests below are about the IDE, Chrome and network entries, so they
    assert on this block rather than on the whole menu: the workflow block that
    precedes it is pinned once, by
    `test_menu_actions_running_offers_the_workflow_verbs`.
    """
    verbs = [verb for _label, verb in actions]
    return verbs[verbs.index("tmux") :]


def test_menu_actions_running_default_hides_ide_and_chrome():
    actions = dashboard.menu_actions(_ctx())
    assert _session_verbs(actions) == ["tmux", "shell", "net loose", "restart", "stop", "destroy"]
    verbs = [a for _, a in actions]
    assert "ide" not in verbs
    assert "chrome" not in verbs


def test_menu_actions_running_ide_enabled_only():
    actions = dashboard.menu_actions(_ctx(ide_enabled=True))
    assert _session_verbs(actions) == [
        "tmux",
        "shell",
        "ide",
        "net loose",
        "restart",
        "stop",
        "destroy",
    ]
    assert "chrome" not in [a for _, a in actions]


def test_menu_actions_running_chrome_enabled_only():
    actions = dashboard.menu_actions(_ctx(chrome_enabled=True))
    assert _session_verbs(actions) == [
        "tmux",
        "shell",
        "chrome",
        "net loose",
        "restart",
        "stop",
        "destroy",
    ]
    assert "ide" not in [a for _, a in actions]


def test_menu_actions_running_both_enabled():
    actions = dashboard.menu_actions(_ctx(ide_enabled=True, chrome_enabled=True))
    assert _session_verbs(actions) == [
        "tmux",
        "shell",
        "ide",
        "chrome",
        "net loose",
        "restart",
        "stop",
        "destroy",
    ]


def test_menu_actions_stopped():
    actions = dashboard.menu_actions(_ctx(state="Stopped"))
    assert [a for _, a in actions] == ["start", "destroy"]


def test_menu_actions_orphan_disabled():
    assert dashboard.menu_actions(_ctx(has_config=False)) == []


def test_menu_actions_orphan_disabled_regardless_of_flags():
    assert (
        dashboard.menu_actions(_ctx(has_config=False, ide_enabled=True, chrome_enabled=True)) == []
    )


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
    assert not any(v.startswith("net ") for v in verbs)


def test_menu_actions_orphan_disabled_even_with_network():
    assert dashboard.menu_actions(_ctx(has_config=False, current_network="strict")) == []


def test_menu_actions_network_entries_ordered_after_chrome_before_restart():
    actions = dashboard.menu_actions(_ctx(ide_enabled=True, chrome_enabled=True))
    assert _session_verbs(actions) == [
        "tmux",
        "shell",
        "ide",
        "chrome",
        "net loose",
        "restart",
        "stop",
        "destroy",
    ]
    verb_to_label = {verb: label for label, verb in actions}
    assert verb_to_label["net loose"] == "Network: loose"


def test_menu_actions_running_includes_open_pr_when_pr_known():
    actions = dashboard.menu_actions(_ctx(pr_number=123))
    assert actions[0] == ("Open PR", "pr --open")


def test_menu_actions_stopped_includes_open_pr_when_pr_known():
    actions = dashboard.menu_actions(_ctx(state="Stopped", pr_number=7))
    assert ("Open PR", "pr --open") in actions
    # still offers the stopped-state actions
    assert [a for _, a in actions if a != "pr --open"] == ["start", "destroy"]


def test_menu_actions_omits_open_pr_when_no_pr():
    running = dashboard.menu_actions(_ctx())
    stopped = dashboard.menu_actions(_ctx(state="Stopped"))
    assert ("Open PR", "pr --open") not in running
    assert ("Open PR", "pr --open") not in stopped


def test_menu_actions_orphan_stays_empty_even_with_pr():
    assert dashboard.menu_actions(_ctx(has_config=False, pr_number=123)) == []


def test_menu_actions_running_offers_the_workflow_verbs():
    """The workflow verbs come before the session verbs, and an unknown git
    status shows both git-bridge entries (hide only a *known* no-op)."""
    verbs = [v for _, v in dashboard.menu_actions(_ctx())]
    assert verbs == [
        "pr",
        "git push",
        "git pull",
        "git diff",
        "tmux",
        "shell",
        "net loose",
        "restart",
        "stop",
        "destroy",
    ]


def test_menu_actions_workflow_labels_name_their_verb():
    labels = {verb: label for label, verb in dashboard.menu_actions(_ctx())}
    assert labels["pr"] == "Create/update PR"
    assert labels["git push"] == "Update from base (git push)"
    assert labels["git pull"] == "Send commits to host (git pull)"
    assert labels["git diff"] == "Show diff (git diff)"


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


def test_pr_refresh_is_dispatched_as_a_printing_verb():
    """PRINTING_VERBS is matched exactly, not by leading token — without its
    own entry the refresh would lose its output in both front-ends."""
    assert "git push --pr" in dashboard.PRINTING_VERBS
    assert dashboard.dispatch_style("git push --pr") == "output"


def test_menu_actions_mount_mode_has_no_workflow_verbs():
    """A mount-mode container has no clone of its own, so every one of these
    would fail in `sync.assert_container_publishable`."""
    verbs = [v for _, v in dashboard.menu_actions(_ctx(mode="mount"))]
    assert verbs == ["tmux", "shell", "net loose", "restart", "stop", "destroy"]


def test_menu_actions_stopped_has_no_workflow_verbs():
    verbs = [v for _, v in dashboard.menu_actions(_ctx(state="Stopped"))]
    assert verbs == ["start", "destroy"]


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
    """Diagnostics first: the corrective and diagnostic entries head the list,
    far from Destroy at the bottom."""
    verbs = [
        v for _, v in dashboard.menu_actions(_ctx(job_clearable=True, has_job=True, pr_number=7))
    ]
    assert verbs[:4] == ["job clear", "job log", "pr --open", "pr"]


def test_menu_actions_orphan_ignores_every_workflow_field():
    assert (
        dashboard.menu_actions(
            _ctx(has_config=False, has_job=True, pr_number=7, git_status=_dirty())
        )
        == []
    )


def test_open_menu_captures_the_actions_with_the_cursor_at_the_top(tmp_path):
    config_path = tmp_path / "config.yaml"
    group = dashboard.RepoGroup("alpha", str(tmp_path), config_path, [_ci("alpha-x", "alpha")])

    menu = dashboard.open_menu([group], "alpha-x")

    assert menu is not None
    assert menu.container == "alpha-x"
    assert menu.index == 0
    assert menu.actions == dashboard.actions_for_container([group], "alpha-x")
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


def test_dispatch_action_runs_jailbee_with_the_repos_config(mocker, tmp_path):
    config_path = tmp_path / "config.yaml"
    run = mocker.patch.object(dashboard.subprocess, "run")
    run.return_value.returncode = 0

    rc = dashboard._dispatch_action(config_path, "net loose", "alpha-x")

    run.assert_called_once_with(
        ["jailbee", "net", "loose", "alpha-x", "--config", str(config_path)], check=False
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
        dashboard._dispatch_action(config_path, verb, "alpha-x")
        run.assert_called_once_with(
            ["jailbee", verb, "alpha-x", "--config", str(config_path), "--force"],
            check=False,
        )


def test_dispatch_action_does_not_force_other_verbs(mocker, tmp_path):
    """`--force` means different things per command (and most don't take it
    at all), so only the attach verbs get it appended."""
    run = mocker.patch.object(dashboard.subprocess, "run")
    run.return_value.returncode = 0

    dashboard._dispatch_action(tmp_path / "config.yaml", "restart", "alpha-x")

    assert "--force" not in run.call_args[0][0]


def test_dispatch_action_reports_the_commands_exit_code(mocker, tmp_path):
    run = mocker.patch.object(dashboard.subprocess, "run")
    run.return_value.returncode = 2

    assert dashboard._dispatch_action(tmp_path / "config.yaml", "tmux", "alpha-x") == 2


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
                    ide_enabled=True,
                    chrome_enabled=True,
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

    rc = dashboard._dispatch_action(config_path, "git diff", "alpha-x")

    assert rc == 0
    producer_argv, viewer_argv = (c.args[0] for c in popen.call_args_list)
    assert producer_argv == [
        "jailbee",
        "git",
        "diff",
        "alpha-x",
        "--config",
        str(config_path),
        "--color",
    ]
    assert viewer_argv == ["less", "-R"]
    run.assert_not_called()  # the paged path replaces the plain run entirely


def test_dispatch_action_pauses_after_a_printing_verb(mocker, tmp_path):
    run = mocker.patch.object(dashboard.subprocess, "run")
    run.return_value.returncode = 0
    wait = mocker.patch.object(dashboard, "_wait_for_return")

    dashboard._dispatch_action(tmp_path / "config.yaml", "git push", "alpha-x")

    wait.assert_called_once_with()


def test_dispatch_action_does_not_pause_after_an_interactive_verb(mocker, tmp_path):
    """tmux and shell end when the user leaves them; there is nothing left to
    read, and an extra keypress would just be in the way."""
    run = mocker.patch.object(dashboard.subprocess, "run")
    run.return_value.returncode = 0
    wait = mocker.patch.object(dashboard, "_wait_for_return")

    dashboard._dispatch_action(tmp_path / "config.yaml", "tmux", "alpha-x")

    wait.assert_not_called()


def test_dispatch_action_falls_back_to_a_pause_when_there_is_no_pager(mocker, tmp_path):
    mocker.patch.object(dashboard, "pager_argv", return_value=None)
    run = mocker.patch.object(dashboard.subprocess, "run")
    run.return_value.returncode = 3
    wait = mocker.patch.object(dashboard, "_wait_for_return")

    rc = dashboard._dispatch_action(tmp_path / "config.yaml", "git diff", "alpha-x")

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

    rc = dashboard._dispatch_action(tmp_path / "config.yaml", "git diff", "alpha-x")

    assert rc == 0
    # Nothing will ever read the pipe, so the first process must not be left
    # blocked on a full one.
    producer.kill.assert_called_once_with()
    run.assert_called_once()
    wait.assert_called_once_with()


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
            ide_enabled=True,
            chrome_enabled=False,
        )
    ]
    expected = menu_actions(_ctx(ide_enabled=True, chrome_enabled=False))
    assert actions_for_container(groups, "p-foo") == expected
    assert actions_for_container(groups, "nope") == []
    assert actions_for_container(groups, None) == []


def test_gather_rows_sets_ide_and_chrome_flags_from_config(tmp_path, mocker, make_cfg):
    cfg = make_cfg(tmp_path / "alpha", jetbrains={"enabled": True}, chrome={"enabled": False})
    path = tmp_path / "alpha" / ".jailbee" / "config.yaml"
    mocker.patch.object(dashboard, "load_config", return_value=cfg)

    def fake_list(c, incus, *, all_repos, with_git_status, with_background):
        return [] if all_repos else [_ci("alpha-one", "alpha")]

    mocker.patch.object(dashboard, "list_containers", side_effect=fake_list)
    groups = dashboard.gather_rows(mocker.MagicMock(), [path], cwd_config=path, with_git=False)
    group = next(g for g in groups if g.prefix == "alpha")
    assert group.ide_enabled is True
    assert group.chrome_enabled is False


def test_gather_rows_orphan_groups_default_ide_chrome_disabled(tmp_path, mocker, make_cfg):
    cfg = make_cfg(tmp_path / "alpha")
    path = tmp_path / "alpha" / ".jailbee" / "config.yaml"
    mocker.patch.object(dashboard, "load_config", return_value=cfg)

    def fake_list(c, incus, *, all_repos, with_git_status, with_background):
        if all_repos:
            return [_ci("alpha-one", "alpha"), _ci("gamma-x", "gamma")]
        return [_ci("alpha-one", "alpha")]

    mocker.patch.object(dashboard, "list_containers", side_effect=fake_list)
    groups = dashboard.gather_rows(mocker.MagicMock(), [path], cwd_config=path, with_git=False)
    orphan = next(g for g in groups if g.prefix == "gamma")
    assert orphan.ide_enabled is False
    assert orphan.chrome_enabled is False


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


def test_visible_fields_network_cell_folds_loose_ttl():
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
    assert network_field.cell(loose) == "loose (12m)"
    assert network_field.cell(strict) == "strict"


def test_network_cell_renders_hours_for_a_long_ttl():
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
    assert network_field.cell(loose) == "loose (2h 5m)"


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
    assert network_field.cell(c) == "loose (—)"


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


def test_visible_fields_still_folds_the_loose_ttl_into_network():
    """The network-cell swap must survive an explicit field list."""
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

    assert network.cell(loose) == "loose (2h)"


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

    gcfg = dashboard._global_config_or_defaults()

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
    `load_config` would stop being uncalled. The previous version of this
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
    load_config = mocker.patch.object(dashboard, "load_config", return_value=repo_cfg)

    state = dashboard.seed_view_state(engine, FRONTEND_TUI)

    assert state.columns == ("name", "state")  # the global block's answer, not the repo's
    load_config.assert_not_called()  # the repo layer is never even read


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
    """`seed_view_state` itself never writes: filtering happens only on the
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


def _render_text(renderable: RenderableType) -> str:
    console = Console(record=True, width=200)
    console.print(renderable)
    return console.export_text()


def test_render_hides_job_column_until_a_job_exists(tmp_path):
    now = datetime(2026, 6, 8, 12, 0, tzinfo=UTC)

    # No containers have an in-flight job -> JOB column hidden.
    # We check that the JOB header is absent from the rendered table headers.
    g_noop = dashboard.RepoGroup(
        "alpha", "/repos/alpha", tmp_path / "a.yaml", [_ci("alpha-one", "alpha")]
    )
    out = _render_text(
        dashboard.render(
            [g_noop], selected=None, now=now, last_refresh_age=1.0, interval=3.0, git_enabled=True
        )
    )
    # The header row must not contain the JOB column header.
    # We check the header line specifically (second line of the output).
    header_line = next(ln for ln in out.splitlines() if "NAME" in ln)
    assert " JOB " not in header_line and not header_line.startswith("JOB ")
    # The cell value "cloning" must also be absent when no job is in flight.
    assert "cloning" not in out

    # A container with an in-flight job -> JOB column present, phase value visible.
    c = _ci("alpha-two", "alpha")
    c.job_phase = "cloning"
    g_op = dashboard.RepoGroup("alpha", "/repos/alpha", tmp_path / "a.yaml", [c])
    out2 = _render_text(
        dashboard.render(
            [g_op], selected=None, now=now, last_refresh_age=1.0, interval=3.0, git_enabled=True
        )
    )
    assert "cloning" in out2
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
            last_refresh_age=1.0,
            interval=3.0,
            git_enabled=True,
        )
    )
    assert "alpha" in out
    assert "gamma" in out and "orphan" in out
    assert "one" in out  # display_name with prefix stripped
    assert "gamma-x" in out
    # footer keybindings present
    assert "Enter" in out and "quit" in out
    # selected row marked with arrow
    assert "▸" in out


def test_render_empty_groups_shows_placeholder():
    out = _render_text(
        dashboard.render(
            [],
            selected=None,
            now=datetime(2026, 6, 8, tzinfo=UTC),
            last_refresh_age=0.0,
            interval=3.0,
            git_enabled=False,
        )
    )
    assert "no containers" in out.lower()
    assert "no-git" in out


def test_render_forwards_enabled_columns_to_visible_fields(tmp_path):
    now = datetime(2026, 6, 8, 12, 0, tzinfo=UTC)
    g = dashboard.RepoGroup(
        "alpha", "/repos/alpha", tmp_path / "a.yaml", [_ci("alpha-one", "alpha")]
    )
    out = _render_text(
        dashboard.render(
            [g],
            selected=None,
            now=now,
            last_refresh_age=1.0,
            interval=3.0,
            git_enabled=True,
            enabled=["name", "created"],
        )
    )
    header_line = next(ln for ln in out.splitlines() if "NAME" in ln)
    assert "CREATED" in header_line
    assert "STATE" not in header_line


def _title_line(groups, *, age: float = 1.0, git_enabled: bool = True, **kwargs) -> str:
    """The panel's top border line, which carries the dashboard title."""
    out = _render_text(
        dashboard.render(
            groups,
            selected=kwargs.pop("selected", None),
            now=datetime(2026, 6, 8, 12, 0, tzinfo=UTC),
            last_refresh_age=age,
            interval=3.0,
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
    assert line.index("jailbee dashboard") <= 3


def test_render_title_refresh_field_is_fixed_width(tmp_path):
    """A one- and a two-digit age must occupy the same number of columns, or
    the title jumps every time the age ticks past 9s."""
    g = dashboard.RepoGroup(
        "alpha", "/repos/alpha", tmp_path / "a.yaml", [_ci("alpha-one", "alpha")]
    )
    fresh = _title_line([g], age=1.0)
    stale = _title_line([g], age=12.0)

    assert "1s/3s" in fresh
    assert "12s/3s" in stale
    # Same amount of border fill => the title text is the same width.
    assert fresh.count("─") == stale.count("─")


def test_render_title_clamps_an_absurd_refresh_age(tmp_path):
    """A stalled gather must not widen the field past two digits."""
    g = dashboard.RepoGroup(
        "alpha", "/repos/alpha", tmp_path / "a.yaml", [_ci("alpha-one", "alpha")]
    )
    assert "99s/3s" in _title_line([g], age=4000.0)


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
            last_refresh_age=1.0,
            interval=3.0,
            git_enabled=True,
        )
    )
    assert "refreshed" not in out


def test_render_keeps_the_table_visible_under_the_menu_overlay(tmp_path):
    """The point of the inline menu: the dashboard stays on screen behind it."""
    g = dashboard.RepoGroup(
        "alpha",
        "/repos/alpha",
        tmp_path / "a.yaml",
        [_ci("alpha-one", "alpha"), _ci("alpha-two", "alpha")],
    )
    menu = dashboard.MenuState(
        "alpha-one", [("Attach tmux", "tmux"), ("Open shell", "shell")], index=1
    )
    out = _render_text(
        dashboard.render(
            [g],
            selected=dashboard.Row("container", "alpha-one"),
            now=datetime(2026, 6, 8, 12, 0, tzinfo=UTC),
            last_refresh_age=1.0,
            interval=3.0,
            git_enabled=True,
            overlay=menu,
        )
    )
    # Both container rows and the column headers are still rendered.
    assert "one" in out and "two" in out
    assert "NAME" in out
    # The menu lists its actions, titled with the target container.
    assert "Attach tmux" in out and "Open shell" in out
    assert "alpha-one" in out
    # The highlighted entry (index=1) carries the cursor, the other does not.
    cursor_line = next(ln for ln in out.splitlines() if "Open shell" in ln)
    other_line = next(ln for ln in out.splitlines() if "Attach tmux" in ln)
    assert "▸" in cursor_line
    assert "▸" not in other_line


def test_render_swaps_the_hint_line_while_the_menu_is_open(tmp_path):
    g = dashboard.RepoGroup(
        "alpha", "/repos/alpha", tmp_path / "a.yaml", [_ci("alpha-one", "alpha")]
    )
    kwargs = {
        "selected": dashboard.Row("container", "alpha-one"),
        "now": datetime(2026, 6, 8, 12, 0, tzinfo=UTC),
        "last_refresh_age": 1.0,
        "interval": 3.0,
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
    assert "Enter" in browsing and "quit" in browsing and "Esc" not in browsing
    assert "Esc" in menu_open and "cancel" in menu_open and "quit" not in menu_open


def test_render_shows_a_notice_and_omits_it_when_none(tmp_path):
    g = dashboard.RepoGroup(
        "alpha", "/repos/alpha", tmp_path / "a.yaml", [_ci("alpha-one", "alpha")]
    )
    kwargs = {
        "selected": None,
        "now": datetime(2026, 6, 8, 12, 0, tzinfo=UTC),
        "last_refresh_age": 1.0,
        "interval": 3.0,
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
                    ide_enabled=True,
                    chrome_enabled=True,
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


def test_quick_verb_follows_the_repos_ide_and_chrome_flags(tmp_path):
    group = dashboard.RepoGroup(
        "alpha",
        str(tmp_path),
        tmp_path / "c.yaml",
        [_ci("alpha-x", "alpha")],
        ide_enabled=True,
        chrome_enabled=True,
    )

    assert dashboard.quick_verb([group], "alpha-x", "action:ide") == "ide"
    assert dashboard.quick_verb([group], "alpha-x", "action:chrome") == "chrome"


def test_quick_reject_note_names_the_key_and_the_container(tmp_path):
    stopped = dashboard.RepoGroup(
        "alpha", str(tmp_path), tmp_path / "c.yaml", [_ci("alpha-x", "alpha", state="Stopped")]
    )

    note = dashboard.quick_reject_note([stopped], "alpha-x", "action:tmux")

    assert "'t'" in note and "tmux" in note and "alpha-x" in note


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
            last_refresh_age=1.0,
            interval=3.0,
            git_enabled=True,
            overlay="help",
        )
    )
    for b in dashboard.KEY_BINDINGS:
        if b.hint:
            assert b.hint in out, f"{b.token}: hint {b.hint!r} missing from help"
            assert b.label in out, f"{b.token}: label {b.label!r} missing from help"
    # Help replaces neither the table nor the hint line, and explains gating.
    assert "NAME" in out and "one" in out
    assert "offered" in out or "available" in out
    assert "close" in out


def test_render_hint_line_is_built_from_the_key_table(tmp_path):
    g = dashboard.RepoGroup(
        "alpha", "/repos/alpha", tmp_path / "a.yaml", [_ci("alpha-one", "alpha")]
    )
    out = _render_text(
        dashboard.render(
            [g],
            selected=None,
            now=datetime(2026, 6, 8, 12, 0, tzinfo=UTC),
            last_refresh_age=1.0,
            interval=3.0,
            git_enabled=True,
        )
    )
    hint_line = next(ln for ln in out.splitlines() if "refresh" in ln and "quit" in ln)
    for b in dashboard.KEY_BINDINGS:
        if b.brief:
            assert b.brief in hint_line, f"{b.token}: {b.brief!r} missing from the hint line"
            assert b.hint in hint_line
    # The rarely-used keys stay in help only, so the line cannot grow unbounded.
    assert "Chrome" not in hint_line


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


def test_refresh_due_schedule():
    # first tick: always gather, git included when enabled
    assert dashboard._refresh_due(
        now=0.0,
        last_base=0.0,
        last_full=0.0,
        interval=3.0,
        git_interval=10.0,
        git_enabled=True,
        first=True,
        forced=False,
    ) == (True, True)
    # forced: gather + git
    assert dashboard._refresh_due(
        now=1.0,
        last_base=1.0,
        last_full=1.0,
        interval=3.0,
        git_interval=10.0,
        git_enabled=True,
        first=False,
        forced=True,
    ) == (True, True)
    # nothing due
    assert dashboard._refresh_due(
        now=2.0,
        last_base=1.0,
        last_full=1.0,
        interval=3.0,
        git_interval=10.0,
        git_enabled=True,
        first=False,
        forced=False,
    ) == (False, False)
    # base due, git not due
    assert dashboard._refresh_due(
        now=5.0,
        last_base=1.0,
        last_full=1.0,
        interval=3.0,
        git_interval=10.0,
        git_enabled=True,
        first=False,
        forced=False,
    ) == (True, False)
    # git due -> base also true
    assert dashboard._refresh_due(
        now=12.0,
        last_base=11.0,
        last_full=1.0,
        interval=3.0,
        git_interval=10.0,
        git_enabled=True,
        first=False,
        forced=False,
    ) == (True, True)
    # git disabled: base due -> (True, False), never git
    assert dashboard._refresh_due(
        now=100.0,
        last_base=1.0,
        last_full=1.0,
        interval=3.0,
        git_interval=10.0,
        git_enabled=False,
        first=False,
        forced=False,
    ) == (True, False)
    assert dashboard._refresh_due(
        now=100.0,
        last_base=1.0,
        last_full=1.0,
        interval=3.0,
        git_interval=10.0,
        git_enabled=False,
        first=True,
        forced=False,
    ) == (True, False)


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
            last_refresh_age=1.0,
            interval=3.0,
            git_enabled=True,
        )
    )
    assert "3.7G" in out  # used
    assert "8GiB" in out  # limit
    assert "MEM" in out  # the new column header
    assert "MEMORY LIMIT" not in out  # bare-limit column was swapped out


def test_dashboard_command_delegates_to_run(mocker):
    run = mocker.patch("jailbee.dashboard.run", return_value=0)
    mocker.patch("jailbee.incus.Incus")
    mocker.patch(
        "jailbee.cli.find_repo_config",
        side_effect=__import__(
            "jailbee.config", fromlist=["ConfigNotFoundError"]
        ).ConfigNotFoundError("none"),
    )
    result = CliRunner().invoke(app, ["dashboard", "-i", "5", "--no-git"])
    assert result.exit_code == 0
    _, kwargs = run.call_args
    assert kwargs["interval"] == 5.0
    assert kwargs["no_git"] is True
    assert kwargs["cwd_config"] is None


def test_tui_command_is_an_alias_for_the_dashboard(mocker):
    """`jailbee tui` mirrors `jailbee gui`: the TUI frontend, same options."""
    run = mocker.patch("jailbee.dashboard.run", return_value=0)
    mocker.patch("jailbee.incus.Incus")
    mocker.patch(
        "jailbee.cli.find_repo_config",
        side_effect=__import__(
            "jailbee.config", fromlist=["ConfigNotFoundError"]
        ).ConfigNotFoundError("none"),
    )
    result = CliRunner().invoke(app, ["tui", "-i", "5", "--git-interval", "7", "--no-git"])
    assert result.exit_code == 0
    _, kwargs = run.call_args
    assert kwargs["interval"] == 5.0
    assert kwargs["git_interval"] == 7.0
    assert kwargs["no_git"] is True


def test_menu_actions_clear_job_entry_is_first_when_clearable():
    actions = dashboard.menu_actions(_ctx(job_clearable=True))
    assert actions[0] == ("Clear failed job", "job clear")


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

    assert verbs[0] == "job clear"


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

    assert verbs[0] == "job clear"


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
    """Space is forgiving: it folds the current row's group whether the cursor
    is on the header or on any container inside it."""
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
            last_refresh_age=1.0,
            interval=3.0,
            git_enabled=True,
            folded=frozenset({"alpha"}),
        )
    )
    # NAME cells show display_name (repo prefix stripped, see ContainerInfo);
    # "beta-two" -> "two" is the neighbour's untouched row, distinct from
    # "alpha-one" -> "one" so the two containers cannot be confused for
    # each other in the assertion below.
    assert "one" not in out  # folded away
    assert "two" in out  # its neighbour is untouched
    assert "▸" in out and "▾" in out  # collapsed and expanded markers both drawn
    assert "1 folded" in out  # the title says what is hidden


def test_render_marks_a_selected_repo_header(tmp_path):
    """A header the cursor sits on must look selected.

    Headers became cursor stops one commit earlier, where `render` did not
    consult `selected` for them at all — so the stop was state-correct and
    invisible, and pressing Down onto a header made the highlight vanish
    with nothing replacing it. The gutter arrow is what marks the cursor row
    for containers; a selected header carries it too, so both kinds of stop
    read as one cursor.
    """
    now = datetime(2026, 6, 8, 12, 0, tzinfo=UTC)
    g = dashboard.RepoGroup("alpha", "/a", tmp_path / "a.yaml", [_ci("alpha-one", "alpha")])
    kwargs = dict(now=now, last_refresh_age=1.0, interval=3.0, git_enabled=True)

    unselected = _render_text(dashboard.render([g], selected=None, **kwargs))
    on_header = _render_text(
        dashboard.render([g], selected=dashboard.Row("repo", "alpha"), **kwargs)
    )

    assert unselected != on_header  # the header renders differently as the cursor row
    # The group is unfolded, so its fold marker is `▾`; `▸` can only be the
    # selection gutter. That makes this assertion discriminating rather than
    # matching whichever glyph happens to be present.
    #
    # The exclusion clause must name what the container row actually
    # contains: `display_name` strips the `<repo>-` prefix, so the row reads
    # "one", never "alpha-one" — filtering on the full name was inert (it
    # excluded nothing, since that string never appears in either line) and
    # would have let the container's own row satisfy this `next()` by
    # accident if it had ever picked up a `▸` of its own.
    header_line = next(ln for ln in on_header.splitlines() if "alpha" in ln and "one" not in ln)
    assert "▸" in header_line


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
            last_refresh_age=1.0,
            interval=3.0,
            git_enabled=True,
            enabled=("state", "network"),  # `name` disabled; `state` is first
        )
    )
    lines = [ln for ln in out.splitlines() if ln.strip()]
    header_line = next(ln for ln in lines if "alpha" in ln)
    data_line = next(ln for ln in lines if "Running" in ln)
    # Both lines start with the Panel's own border+padding ("│ "), identical
    # on every row, so strip exactly that one border character before
    # measuring each row's own indentation — comparing the raw lines
    # (border included) would always read indent 0 for both, since neither
    # starts with a literal space.
    header_indent = len(header_line[1:]) - len(header_line[1:].lstrip(" "))
    data_indent = len(data_line[1:]) - len(data_line[1:].lstrip(" "))
    assert header_indent == data_indent


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
            last_refresh_age=1.0,
            interval=3.0,
            git_enabled=True,
            folded=frozenset({"alpha"}),
        )
    )
    assert "2 containers" in out


def test_show_if_is_computed_from_visible_containers_only(tmp_path):
    """A folded group must not keep alive a column that has nothing to say on
    screen. The PR container is folded away, so the PR column goes with it."""
    now = datetime(2026, 6, 8, 12, 0, tzinfo=UTC)
    with_pr = _ci("alpha-one", "alpha")
    with_pr.pr_number = 7
    groups = [
        dashboard.RepoGroup("alpha", "/a", tmp_path / "a.yaml", [with_pr]),
        dashboard.RepoGroup("beta", "/b", tmp_path / "b.yaml", [_ci("beta-one", "beta")]),
    ]
    kwargs = dict(selected=None, now=now, last_refresh_age=1.0, interval=3.0, git_enabled=True)
    unfolded = _render_text(dashboard.render(groups, **kwargs))
    folded = _render_text(dashboard.render(groups, folded=frozenset({"alpha"}), **kwargs))

    assert "PR" in unfolded
    assert "PR" not in folded


def test_fold_key_is_bound_to_space_and_documented():
    """A key that is not in KEY_BINDINGS is invisible in the help overlay and
    the hint line, which is how the two used to drift."""
    assert dashboard.parse_key(b" ") == "fold"
    binding = dashboard.binding_for_token("fold")
    assert binding is not None
    assert binding.hint and binding.label


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
    assert dashboard.dynamic_column_names() == frozenset({"job", "ttl", "pr", "mode"})


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
    assert "empty" not in prefixes  # draws nothing, folds nothing
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
            last_refresh_age=1.0,
            interval=3.0,
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


def _drive_run(mocker, key_sequence: list[bytes]) -> int:
    """Run the real ``dashboard.run()`` key loop with a fake terminal.

    Feeds ``key_sequence`` one key per main-loop iteration (``os.read`` is
    mocked, not stdin itself), padded with a trailing Ctrl-C so the loop
    always terminates even if a test's own key list doesn't. Everything
    that would touch a real terminal, the state DB, or Incus is mocked;
    ``gather_live`` returns no containers, so these tests exercise overlay
    and persistence behaviour without depending on the background
    refresher thread ever publishing a snapshot before the key loop reads
    it (a real race the tests must not depend on winning).
    """
    mocker.patch.object(
        dashboard, "collect_config_paths", return_value=[Path("/x/.jailbee/config.yaml")]
    )
    mocker.patch("jailbee.db.get_engine", return_value=mocker.Mock())
    mocker.patch.object(dashboard, "seed_view_state", return_value=dashboard.ViewState())
    mocker.patch.object(dashboard, "gather_live", return_value=[])

    mock_stdin = mocker.Mock()
    mock_stdin.isatty.return_value = True
    mock_stdin.fileno.return_value = 0
    mocker.patch.object(dashboard.sys, "stdin", mock_stdin)
    mock_stdout = mocker.Mock()
    mock_stdout.isatty.return_value = True
    mocker.patch.object(dashboard.sys, "stdout", mock_stdout)

    mocker.patch.object(dashboard.termios, "tcgetattr", return_value=object())
    mocker.patch.object(dashboard.termios, "tcsetattr")
    mocker.patch.object(dashboard.tty, "setcbreak")
    mocker.patch.object(dashboard.select, "select", return_value=([True], [], []))

    padded = itertools.chain(key_sequence, [b"\x03"], itertools.repeat(b"\x03"))
    mocker.patch.object(dashboard.os, "read", side_effect=lambda fd, n: next(padded))

    return dashboard.run(mocker.Mock(), None, interval=0.5, git_interval=1.0, no_git=True)


def test_run_degrades_when_save_view_state_fails(mocker):
    """A DB write failure on the keypress path (fold key, Enter on a header,
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
    # cursor (a fold-key press on the Fields tab) — one of the three
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


def test_settings_key_switches_from_another_overlay_instead_of_closing(mocker):
    """F2/S must mirror ``h``'s own toggle: pressing it while another
    overlay (the action menu, help) is open switches to settings, not just
    closes whatever was open.

    There is no live group in this harness (``gather_live`` returns
    ``[]``), so a bare-table fold keypress (`Space` with no overlay open)
    has nothing to act on and never reaches ``save_view_state`` — see
    ``fold_target``. That makes ``save_view_state`` firing after
    ``h`` then ``S`` then `Space` a discriminating signal that ``S``
    actually opened the settings overlay (whose own `Space`/fold handling
    unconditionally calls ``persist_view_state``), rather than merely
    closing help and leaving the bare table's fold key to reject the
    keypress silently. Fails if `"settings"` goes back to being grouped
    with `("cancel", "quit")`, which only closes whatever overlay is open.
    """
    save = mocker.patch.object(dashboard, "save_view_state")
    rc = _drive_run(mocker, [b"h", b"S", b" "])

    assert rc == 0
    save.assert_called_once()
